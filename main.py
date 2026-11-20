"""实现 QQ Agently Mail 的登录会话承载、账号绑定指令与 LLM 工具面。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4
import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.util import SessionController, session_waiter
from astrbot.api.star import Context, Star

from .agentlymail.client import AgentlyMailClient, ClientError
from .agentlymail.constants import API_BASE, AUTH_BASE
from .agentlymail.headless import (
    Func3Result,
    HeadlessQRLogin,
    HeadlessQRLoginError,
    HeadlessQRSession,
)
from .agentlymail.models import Me
from .agentlymail.oauth import (
    AccessDeniedError,
    AuthorizationPendingError,
    DeviceFlowStart,
    ExpiredTokenError,
    OAuthClient,
    OAuthError,
)
from .agentlymail.render import format_new_mail_notice
from .agentlymail.session import LoginRegistry
from .agentlymail.token_store import TokenBundle, TokenStore, TokenStoreError
from .agentlymail.tools import make_tools
from .agentlymail.watcher import MAX_SEEN_MESSAGE_IDS, MessageWatcher
from .agentlymail.web_api import register_web_apis

# 设备流 expires_in 固定为 600 秒，作为登录防重入的过期上限。
LOGIN_FLOW_TTL_SECONDS = 600.0

# KV 中会话到账号的绑定表键名，值为 {unified_msg_origin: 账号名}。
BINDINGS_KEY = "umo_bindings"

# KV 中本地发送计数的键名，值为 {账号名: {date: "YYYY-MM-DD", count: N}}。
SEND_COUNTER_KEY = "send_counter"


# KV 中每个会话唯一的新邮件监听配置。
WATCH_CONFIG_KEY = "watch_configs"


@dataclass(slots=True)
class _WatchTaskEntry:
    """登记一个监听任务及其持久化代次。"""

    task: asyncio.Task
    account: str
    alias_id: str
    watch_id: str

@filter.command_group("agently")
def agently_group():
    """QQ Agently Mail 邮箱指令组。"""


class QQAgentlyMailPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self._config = config if config is not None else {}
        self._token_store = TokenStore()
        self._registry = LoginRegistry()
        # 代理环境变量会打断 agent.qq.com 的授权链路，两个客户端都直连。
        # 代理环境变量会打断 agent.qq.com 的授权链路，两个客户端都直连；
        # 设备流 poll 服务端长挂约 25 秒，读超时须覆盖两次保持窗口。
        self._api_http = httpx.AsyncClient(base_url=API_BASE, trust_env=False)
        self._auth_http = httpx.AsyncClient(
            base_url=AUTH_BASE,
            trust_env=False,
            timeout=httpx.Timeout(90, connect=10),
        )
        self._clients: dict[str, tuple[AgentlyMailClient, OAuthClient]] = {}
        self._me_cache: dict[str, Me] = {}
        self._tasks: set[asyncio.Task] = set()
        self._watch_tasks: dict[str, _WatchTaskEntry] = {}
        self._watch_config_lock = asyncio.Lock()
        self._send_counter_lock = asyncio.Lock()

    async def initialize(self):
        """注册工具与 Web API，并恢复已持久化的新邮件监听。"""
        tools = make_tools(self)
        self.context.add_llm_tools(*tools)
        logger.info(f"[agently] LLM 工具注册完成：数量={len(tools)}")
        register_web_apis(self)
        logger.debug("[agently] 插件初始化开始恢复新邮件监听")
        try:
            await self._restore_watches()
        except Exception:
            logger.exception("恢复新邮件监听失败")

    async def terminate(self):
        """插件卸载时取消全部后台任务并关闭共享 HTTP 客户端。"""
        logger.debug(f"[agently] 插件卸载开始：后台任务数量={len(self._tasks)}")
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.debug("[agently] 插件后台任务已取消并清理")
        await self._auth_http.aclose()
        await self._api_http.aclose()
        logger.info("[agently] 插件卸载完成")

    @agently_group.command("login")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def login(self, event: AstrMessageEvent, account: str = "default"):
        """发起扫码登录，可带账号名，缺省 default"""
        error = await self._start_login(event, account)
        if error is not None:
            await event.send(event.plain_result(error))

    @agently_group.command("logout")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def logout(self, event: AstrMessageEvent, account: str = "default"):
        """删除账号凭证并解绑引用它的会话，可带账号名，缺省 default"""
        deleted, unbound, error = await self._remove_account(account)
        if error is not None:
            await event.send(event.plain_result(error))
            return
        if not deleted:
            await event.send(event.plain_result(f"账号 {account} 没有已保存的凭证。"))
            return
        await event.send(
            event.plain_result(
                f"已删除账号 {account} 的凭证，并解除 {unbound} 个会话的绑定。",
            ),
        )
    @agently_group.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def status(self, event: AstrMessageEvent):
        """列出全部账号并标注当前会话的绑定"""
        snapshot = await self._account_snapshot(event.unified_msg_origin)
        current = snapshot["current_binding"]
        lines = []
        for item in snapshot["accounts"]:
            if "invalid" in item:
                lines.append(f"- {item['account']}：凭证文件无效（{item['invalid']}）")
                continue
            mark = "（当前会话绑定）" if item["account"] == current else ""
            lines.append(
                f"- {item['account']}{mark}：邮箱 {item['email']}，"
                f"凭证有效期至 {item['expires_at']}，app_id {item['app_id'] or '未知'}",
            )
        parts = []
        if lines:
            parts.append("已保存凭证的账号：\n" + "\n".join(lines))
        else:
            parts.append("还没有任何账号凭证，请执行 /agently login。")
        if current is not None:
            parts.append(f"当前会话绑定账号：{current}。")
        else:
            parts.append("当前会话未绑定账号。")
        if snapshot["login_in_flight"]:
            parts.append("当前会话有登录在途，请等待结果推送。")
        await event.send(event.plain_result("\n".join(parts)))

    @agently_group.command("bind")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bind(self, event: AstrMessageEvent, account: str):
        """把当前会话绑定到指定账号"""
        if await self._token_store.load(account) is None:
            logger.warning(f"[agently] 绑定失败：账号={account}，原因=凭证不存在")
            await event.send(
                event.plain_result(
                    f"账号 {account} 没有已保存的凭证，请先执行 /agently login {account}。",
                ),
            )
            return
        bindings = await self._load_bindings()
        umo = event.unified_msg_origin
        previous = bindings.get(umo)
        if previous != account:
            logger.debug(
                f"[agently] 绑定账号变化，准备停止旧监听：账号={account}，原账号={previous}"
            )
            _, error = await self._stop_watch(umo)
            if error is not None:
                await event.send(event.plain_result(f"绑定失败：{error}"))
                return
        bindings[umo] = account
        await self._save_bindings(bindings)
        logger.info(f"[agently] 会话绑定完成：账号={account}，绑定数量={len(bindings)}")
        await event.send(event.plain_result(f"已把当前会话绑定到账号 {account}。"))

    @agently_group.command("unbind")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def unbind(self, event: AstrMessageEvent):
        """解除当前会话的账号绑定"""
        bindings = await self._load_bindings()
        umo = event.unified_msg_origin
        account = bindings.get(umo)
        _, error = await self._stop_watch(umo)
        if error is not None:
            await event.send(event.plain_result(f"解除绑定失败：{error}"))
            return
        if account is None:
            logger.debug("[agently] 解除绑定跳过：当前会话未绑定账号")
            await event.send(event.plain_result("当前会话未绑定账号。"))
            return
        bindings.pop(umo, None)
        await self._save_bindings(bindings)
        logger.info(f"[agently] 会话解绑完成：账号={account}，绑定数量={len(bindings)}")
        await event.send(event.plain_result(f"已解除当前会话与账号 {account} 的绑定。"))

    def _clients_for(self, account: str) -> tuple[AgentlyMailClient, OAuthClient]:
        """返回账号的客户端对，按需创建并缓存。"""
        pair = self._clients.get(account)
        if pair is None:
            logger.debug(f"[agently] 创建账号客户端缓存：账号={account}")
            oauth = OAuthClient(self._token_store, account, self._auth_http)
            client = AgentlyMailClient(
                self._token_store,
                account,
                self._api_http,
                oauth,
            )
            pair = (client, oauth)
            self._clients[account] = pair
        return pair

    async def _start_login(self, event: AstrMessageEvent, account: str) -> str | None:
        """发起设备流登录并把扫码通知发到会话，返回错误文本或 None。"""
        umo = event.unified_msg_origin
        logger.info(f"[agently] 登录发起：账号={account}")
        if not await self._registry.start(umo, LOGIN_FLOW_TTL_SECONDS):
            logger.warning(f"[agently] 登录发起拒绝：账号={account}，原因=已有登录在途")
            return "该会话已有登录在途，请等待结果推送或过期后再试。"
        _, oauth = self._clients_for(account)
        try:
            flow = await oauth.start()
        except OAuthError as exc:
            await self._registry.finish(umo)
            logger.warning(
                f"[agently] 登录发起失败：账号={account}，异常=OAuthError，代码={exc.code}"
            )
            return f"登录发起失败：{exc.message}"
        except Exception as exc:
            await self._registry.finish(umo)
            logger.exception(f"账号 {account} 发起设备流失败")
            return f"登录发起失败：{exc}"
        logger.debug(
            f"[agently] 设备流初始化完成：账号={account}，有效期={flow.expires_in:.0f}秒"
        )
        deadline = time.monotonic() + flow.expires_in
        headless: HeadlessQRLogin | None = None
        qr_session: HeadlessQRSession | None = None
        user_code: str | None = None
        try:
            user_code = HeadlessQRLogin.user_code_from_browser_url(flow.browser_url)
            headless = HeadlessQRLogin()
            qr_session = await headless.create_qr_session(deadline=deadline)
        except HeadlessQRLoginError as exc:
            logger.warning(
                f"[agently] 无头二维码创建失败，降级浏览器授权：账号={account}，代码={exc.code}"
            )
            if headless is not None:
                await self._close_headless(account, headless)
            headless = None
            qr_session = None
        except Exception:
            logger.exception(f"账号 {account} 创建无头二维码异常，降级发送浏览器链接")
            if headless is not None:
                await self._close_headless(account, headless)
            headless = None
            qr_session = None

        fallback_notice = (
            f"账号 {account} 登录发起成功。请用浏览器打开以下链接，扫码并在手机上点确定：\n"
            f"{flow.browser_url}\n"
            f"验证码：{flow.input_code}\n"
            "二维码约 2-3 分钟过期，过期后请重新执行登录；完成后会自动推送结果。"
        )
        if qr_session is None or headless is None:
            notice = fallback_notice
        else:
            notice = (
                f"账号 {account} 登录发起成功，请使用下方二维码扫码并在手机上点确定。\n"
                f"{qr_session.expires_hint}\n"
                f"备用链接：{flow.browser_url}\n"
                f"验证码：{flow.input_code}\n"
                "扫码完成后会自动推送结果。"
            )

        try:
            if qr_session is not None and headless is not None:
                await self._send_login_notice_with_qr(
                    event,
                    MessageChain().message(notice),
                    qr_session.image,
                )
            else:
                await event.send(MessageChain().message(notice))
        except Exception as exc:
            if qr_session is not None and headless is not None:
                logger.warning(
                    f"[agently] 二维码通知发送失败，改发浏览器授权通知：账号={account}，"
                    f"异常={type(exc).__name__}"
                )
                await self._close_headless(account, headless)
                headless = None
                qr_session = None
                try:
                    await event.send(MessageChain().message(fallback_notice))
                except Exception as fallback_exc:
                    await self._registry.finish(umo)
                    logger.exception(f"账号 {account} 的登录通知发送失败")
                    return f"登录发起成功，但通知发送失败：{fallback_exc}"
            else:
                await self._registry.finish(umo)
                logger.exception(f"账号 {account} 的登录通知发送失败")
                return f"登录发起成功，但通知发送失败：{exc}"

        task = asyncio.create_task(
            self._complete_device_flow(
                event,
                account,
                flow,
                deadline=deadline,
                headless=headless,
                qr_session=qr_session,
                user_code=user_code,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info(
            f"[agently] 登录通知已发送并启动后台授权：账号={account}，"
            f"扫码模式={'二维码' if qr_session is not None else '浏览器'}"
        )
        return None

    async def _send_login_notice_with_qr(
        self,
        event: AstrMessageEvent,
        chain: MessageChain,
        image_bytes: bytes,
    ) -> None:
        """把二维码写入临时文件发送，用完立即删除。"""
        fd, temporary = tempfile.mkstemp(prefix="agently-qr-", suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(image_bytes)
            chain.chain.append(Image.fromFileSystem(temporary))
            await event.send(chain)
        finally:
            os.unlink(temporary)

    async def _close_headless(self, account: str, headless: HeadlessQRLogin) -> None:
        """关闭无头登录客户端，清理失败只记录日志。"""
        try:
            await headless.close()
        except Exception:
            logger.exception(f"账号 {account} 关闭无头登录客户端失败")

    async def _complete_device_flow(
        self,
        event: AstrMessageEvent,
        account: str,
        flow: DeviceFlowStart,
        *,
        deadline: float,
        headless: HeadlessQRLogin | None,
        qr_session: HeadlessQRSession | None,
        user_code: str | None,
    ) -> None:
        """后台完成无头二维码和设备流授权，完成后向发起会话回推结果。"""
        umo = event.unified_msg_origin
        client, oauth = self._clients_for(account)
        logger.debug(
            f"[agently] 后台授权开始：账号={account}，"
            f"扫码模式={'浏览器' if headless is None else '二维码'}"
        )
        try:
            if headless is None:
                bundle = await oauth.poll(flow, deadline=deadline)
            else:
                if qr_session is None or user_code is None:
                    raise HeadlessQRLoginError(
                        "invalid_state",
                        "无头二维码登录缺少设备流参数",
                    )

                async def on_qr_status(status_code: int) -> None:
                    if status_code == 404:
                        await self._push(umo, "已扫码，请在手机上确认。")

                wx_code = await headless.poll_wx_code(
                    qr_session,
                    deadline,
                    on_status=on_qr_status,
                )
                func3_result = await headless.func3(
                    user_code,
                    wx_code,
                    deadline=deadline,
                )
                email = await self._select_agent_mail(
                    event,
                    func3_result,
                    deadline,
                )
                await headless.func8(
                    user_code,
                    email,
                    func3_result.login_tmpticket,
                    deadline=deadline,
                )
                bundle = await oauth.poll(flow, deadline=deadline)
        except asyncio.CancelledError:
            logger.debug(f"[agently] 后台授权已取消：账号={account}")
            raise
        except HeadlessQRLoginError as exc:
            logger.warning(
                f"[agently] 无头登录失败：账号={account}，代码={exc.code}"
            )
            await self._push(
                umo,
                f"账号 {account} 登录失败：{self._headless_error_message(exc)}",
            )
        except AccessDeniedError as exc:
            logger.warning(f"[agently] 用户拒绝设备授权：账号={account}")
            await self._push(umo, f"账号 {account} 登录失败：{exc.message}。")
        except ExpiredTokenError as exc:
            logger.warning(f"[agently] 设备授权已过期：账号={account}")
            await self._push(
                umo,
                f"账号 {account} 登录失败：{exc.message}，"
                "二维码有效期约 2-3 分钟，请重新执行 /agently login。",
            )
        except AuthorizationPendingError as exc:
            logger.warning(f"[agently] 设备授权等待超时：账号={account}")
            await self._push(
                umo,
                f"账号 {account} 登录超时：{exc.message}，请重新执行 /agently login。",
            )
        except OAuthError as exc:
            logger.warning(f"[agently] OAuth 授权失败：账号={account}，代码={exc.code}")
            await self._push(umo, f"账号 {account} 登录失败：{exc.message}。")
        except Exception as exc:
            logger.exception(f"账号 {account} 的设备流轮询异常")
            await self._push(
                umo,
                f"账号 {account} 登录失败：发生未预期的错误（{exc}），详情见插件日志。",
            )
        else:
            logger.info(f"[agently] 登录授权完成，开始收尾绑定：账号={account}")
            try:
                await self._announce_login_success(umo, account, bundle, client)
            except Exception:
                logger.exception(f"账号 {account} 登录成功后的回推或绑定失败")
                await self._push(
                    umo,
                    f"账号 {account} 登录成功，但收尾步骤出错，"
                    "请执行 /agently status 检查。",
                )
        finally:
            logger.debug(f"[agently] 后台授权进入清理：账号={account}")
            if headless is not None:
                await self._close_headless(account, headless)
            await self._registry.finish(umo)

    async def _select_agent_mail(
        self,
        event: AstrMessageEvent,
        result: Func3Result,
        deadline: float,
    ) -> str:
        """自动或在当前会话中选择 func=3 返回的邮箱。"""
        if len(result.mails) == 1:
            mail = result.mails[0]
            if not mail.is_selected:
                raise HeadlessQRLoginError(
                    "invalid_response",
                    "唯一可用邮箱没有 is_selected 标记",
                )
            return mail.email

        lines = ["检测到多个可用邮箱，请在 120 秒内回复邮箱编号："]
        for index, mail in enumerate(result.mails, start=1):
            selected = "（服务端默认）" if mail.is_selected else ""
            lines.append(f"{index}. {mail.email}{selected}")
        lines.append("回复“取消”可终止本次登录。")
        await event.send(event.plain_result("\n".join(lines)))

        choice: dict[str, str] = {}
        initiator_id = event.get_sender_id()

        @session_waiter(timeout=120)
        async def mail_waiter(
            controller: SessionController,
            incoming: AstrMessageEvent,
        ) -> None:
            if incoming.get_sender_id() != initiator_id:
                return
            text = incoming.message_str.strip()
            if text in {"取消", "c", "cancel"}:
                choice["error"] = "cancelled"
                controller.stop()
                return
            if not text.isdecimal():
                await incoming.send(
                    incoming.plain_result("请回复列表中的邮箱编号，或回复“取消”。")
                )
                return
            index = int(text) - 1
            if not 0 <= index < len(result.mails):
                await incoming.send(
                    incoming.plain_result("邮箱编号无效，请重新回复列表中的编号。")
                )
                return
            choice["email"] = result.mails[index].email
            controller.stop()

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HeadlessQRLoginError("expired", "无头二维码登录已超时")
        try:
            await asyncio.wait_for(mail_waiter(event), timeout=min(120.0, remaining))
        except TimeoutError as exc:
            raise HeadlessQRLoginError("mail_timeout", "邮箱选择超时") from exc

        if choice.get("error") == "cancelled":
            raise HeadlessQRLoginError("cancelled", "用户已取消登录")
        email = choice.get("email")
        if email is None:
            raise HeadlessQRLoginError("invalid_state", "邮箱选择未返回结果")
        return email

    @staticmethod
    def _headless_error_message(error: HeadlessQRLoginError) -> str:
        """把无头登录协议错误转换为可执行的用户提示。"""
        if error.code == "expired":
            return "登录流程已超时，请重新执行 /agently login。"
        if error.code == "mail_timeout":
            return "邮箱选择已超时，请重新执行 /agently login。"
        if error.code == "cancelled":
            return "用户已取消登录。"
        if error.code == "-601":
            return "微信登录返回 -601，系统失败或邮箱为空，请重新执行 /agently login。"
        if error.code == "-75111":
            return "微信登录返回 -75111，扫码链接已使用或流程不匹配，请重新执行 /agently login。"
        if error.code == "-5012":
            return "微信登录返回 -5012，登录票据已过期或已消费，请重新执行 /agently login。"
        return f"{error.message}。"

    async def _announce_login_success(
        self,
        umo: str,
        account: str,
        bundle: TokenBundle,
        client: AgentlyMailClient,
    ) -> None:
        """登录成功后绑定发起会话、读取邮箱信息并回推。"""
        # 凭证已在 poll 内保存，切换绑定前先清理当前会话的旧监听。
        bindings = await self._load_bindings()
        if bindings.get(umo) != account:
            _, error = await self._stop_watch(umo)
            if error is not None:
                await self._push(
                    umo,
                    f"账号 {account} 登录成功，但切换会话绑定失败：{error}\n"
                    "未修改当前会话绑定。",
                )
                return
        bindings[umo] = account
        await self._save_bindings(bindings)
        logger.info(f"[agently] 登录成功并绑定会话：账号={account}")
        try:
            me = await client.me()
        except ClientError as exc:
            logger.warning(
                f"[agently] 登录成功但读取邮箱信息失败：账号={account}，"
                f"代码={exc.code}，状态码={exc.status_code}"
            )
            await self._push(
                umo,
                f"账号 {account} 登录成功：app_id {bundle.app_id or '未知'}，"
                f"但读取邮箱信息失败：{exc}\n"
                f"已自动把当前会话绑定到账号 {account}。",
            )
            return
        self._me_cache[account] = me
        limits = json.dumps(me.rate_limits.values, ensure_ascii=False)
        logger.debug(
            f"[agently] 登录后邮箱信息已缓存：账号={account}，地址数量={len(me.aliases)}"
        )
        email = self._primary_email(me)
        await self._push(
            umo,
            f"账号 {account} 登录成功：邮箱 {email}（app_id {bundle.app_id or '未知'}）\n"
            f"限额 {limits}\n"
            f"已自动把当前会话绑定到账号 {account}。",
        )

    async def _stop_watches_for_account(self, account: str) -> list[str]:
        """清理账号的活动及持久化监听，返回所有清理错误。"""
        umos = {
            umo
            for umo, entry in self._watch_tasks.items()
            if entry.account == account
        }
        config_error = None
        async with self._watch_config_lock:
            try:
                configs = await self._read_watch_configs_locked()
            except Exception as exc:
                logger.exception(f"读取账号 {account} 的监听配置失败")
                configs = None
                config_error = f"读取持久化配置失败：{exc}"
            if configs is None:
                config_error = config_error or "持久化配置根数据损坏，未修改原数据。"
            else:
                for umo, raw_config in configs.items():
                    config = self._validate_watch_config(umo, raw_config)
                    if config is not None and config["account"] == account:
                        umos.add(umo)
                    elif (
                        isinstance(umo, str)
                        and isinstance(raw_config, dict)
                        and raw_config.get("account") == account
                    ):
                        umos.add(umo)

        errors = []
        for umo in sorted(umos):
            _, error = await self._stop_watch(umo)
            if error is not None:
                errors.append(f"会话 {umo}：{error}")
        if config_error is not None and not errors:
            errors.append(config_error)
        return errors

    async def _remove_account(self, account: str) -> tuple[bool, int, str | None]:
        """先清理监听，再删除凭证和绑定，并返回明确清理错误。"""
        logger.debug(f"[agently] 账号登出开始：账号={account}")
        cleanup_errors = await self._stop_watches_for_account(account)
        if cleanup_errors:
            return (
                False,
                0,
                f"账号 {account} 登出失败：{'；'.join(cleanup_errors)}",
            )

        logger.debug(f"[agently] 账号监听清理完成：账号={account}")
        if not await self._token_store.delete(account):
            return False, 0, None
        self._clients.pop(account, None)
        self._me_cache.pop(account, None)
        bindings = await self._load_bindings()
        referenced = [
            bound_umo
            for bound_umo, bound_account in bindings.items()
            if bound_account == account
        ]
        for bound_umo in referenced:
            del bindings[bound_umo]
        await self._save_bindings(bindings)
        logger.info(
            f"[agently] 账号登出完成：账号={account}，解除绑定数量={len(referenced)}"
        )
        return True, len(referenced), None

    async def _account_snapshot(self, umo: str) -> dict:
        """枚举账号凭证与当前会话的绑定状态，供命令与工具共用。"""
        accounts = await self._token_store.list_accounts()
        bindings = await self._load_bindings()
        items = []
        for account in accounts:
            try:
                bundle = await self._token_store.load(account)
            except TokenStoreError as exc:
                items.append({"account": account, "invalid": str(exc)})
                continue
            if bundle is None:
                continue
            items.append(
                {
                    "account": account,
                    "email": self._primary_email(self._me_cache.get(account)),
                    "expires_at": time.strftime(
                        "%Y-%m-%d %H:%M",
                        time.localtime(bundle.expires_at),
                    ),
                    "app_id": bundle.app_id,
                }
            )
        return {
            "accounts": items,
            "current_binding": bindings.get(umo),
            "login_in_flight": await self._registry.in_flight(umo),
        }

    async def _read_watch_configs_locked(self) -> dict[str, Any] | None:
        """在监听锁内读取原始配置，根数据损坏时返回 None。"""
        configs = await self.get_kv_data(WATCH_CONFIG_KEY, {})
        if configs is None:
            configs = {}
        if not isinstance(configs, dict):
            logger.error("新邮件监听配置根数据不是对象，未修改原数据")
            return None
        logger.debug(
            f"[agently] 监听配置读取完成：键={WATCH_CONFIG_KEY}，数量={len(configs)}"
        )
        return dict(configs)

    async def _write_watch_configs_locked(self, configs: dict[str, Any]) -> None:
        """在监听锁内写回配置，并保留其他会话的原始记录。"""
        logger.debug(
            f"[agently] 监听配置保存开始：键={WATCH_CONFIG_KEY}，数量={len(configs)}"
        )
        await self.put_kv_data(WATCH_CONFIG_KEY, configs)
        logger.debug(
            f"[agently] 监听配置保存完成：键={WATCH_CONFIG_KEY}，数量={len(configs)}"
        )

    def _validate_watch_config(self, umo: Any, config: Any) -> dict[str, Any] | None:
        """校验并规范化一条监听配置，坏记录返回 None。"""
        if not isinstance(umo, str) or not umo:
            logger.error(f"新邮件监听配置的会话键无效: {umo!r}")
            return None
        if not isinstance(config, dict):
            logger.error(f"会话 {umo} 的监听配置不是对象: {type(config).__name__}")
            return None

        required = ("watch_id", "account", "alias_id", "next_cursor", "seen_message_ids")
        missing = [field for field in required if field not in config]
        if missing:
            logger.error(f"会话 {umo} 的监听配置缺少字段: {', '.join(missing)}")
            return None

        for field in ("watch_id", "account", "alias_id"):
            value = config[field]
            if not isinstance(value, str) or not value.strip():
                logger.error(
                    f"会话 {umo} 的监听配置字段 {field} 无效: {value!r}；"
                    f"账号={config['account']!r}，地址ID={config['alias_id']!r}"
                )
                return None

        cursor = config["next_cursor"]
        if cursor is not None and not isinstance(cursor, str):
            logger.error(
                f"会话 {umo} 的监听配置字段 next_cursor 类型无效；"
                f"账号={config['account']!r}，地址ID={config['alias_id']!r}"
            )
            return None

        seen = config["seen_message_ids"]
        if not isinstance(seen, list) or any(
            not isinstance(message_id, str) or not message_id for message_id in seen
        ):
            logger.error(
                f"会话 {umo} 的监听配置字段 seen_message_ids 无效；"
                f"账号={config['account']!r}，地址ID={config['alias_id']!r}"
            )
            return None

        normalized_seen = self._normalize_watch_seen(seen)
        if normalized_seen != seen:
            logger.warning(
                f"会话 {umo} 的监听去重记录已按顺序去重并截断到 "
                f"{MAX_SEEN_MESSAGE_IDS} 项"
            )

        return {
            "watch_id": config["watch_id"],
            "account": config["account"],
            "alias_id": config["alias_id"],
            "next_cursor": cursor,
            "seen_message_ids": normalized_seen,
        }

    @staticmethod
    def _normalize_watch_seen(seen_message_ids: list[str]) -> list[str]:
        """按最后出现顺序去重，并只保留末尾的有界记录。"""
        ordered: dict[str, None] = {}
        for message_id in seen_message_ids:
            ordered.pop(message_id, None)
            ordered[message_id] = None
        return list(ordered)[-MAX_SEEN_MESSAGE_IDS:]

    async def _save_watch_state(
        self,
        umo: str,
        watch_id: str,
        account: str,
        alias_id: str,
        next_cursor: str | None,
        seen_message_ids: list[str],
    ) -> None:
        """只保存仍属于当前监听代次和账号地址的候选状态。"""
        logger.debug(
            f"[agently] 监听状态保存候选：账号={account}，地址ID={alias_id}，"
            f"游标存在={next_cursor is not None}，去重数量={len(seen_message_ids)}"
        )
        async with self._watch_config_lock:
            configs = await self._read_watch_configs_locked()
            if configs is None:
                raise RuntimeError("新邮件监听配置根数据损坏，未保存监听状态")
            if umo not in configs:
                logger.debug(f"会话 {umo} 的监听配置已删除，丢弃旧任务状态")
                return

            current = self._validate_watch_config(umo, configs[umo])
            if current is None:
                raise RuntimeError(f"会话 {umo} 的监听配置损坏，未保存监听状态")
            if current["watch_id"] != watch_id:
                logger.debug(f"会话 {umo} 的监听代次已变化，丢弃旧任务状态")
                return
            if current["account"] != account or current["alias_id"] != alias_id:
                logger.debug(f"会话 {umo} 的监听账号或地址已变化，丢弃旧任务状态")
                return

            candidate = self._validate_watch_config(
                umo,
                {
                    "watch_id": watch_id,
                    "account": account,
                    "alias_id": alias_id,
                    "next_cursor": next_cursor,
                    "seen_message_ids": seen_message_ids,
                },
            )
            if candidate is None:
                raise ValueError(f"会话 {umo} 的候选监听状态无效")
            configs[umo] = candidate
            await self._write_watch_configs_locked(configs)
            logger.debug(
                f"[agently] 监听状态保存完成：账号={account}，地址ID={alias_id}，"
                f"游标存在={next_cursor is not None}，去重数量={len(seen_message_ids)}"
            )

    @staticmethod
    def _new_watch_id() -> str:
        """生成一次监听启动的唯一代次标识。"""
        return uuid4().hex

    @staticmethod
    def _select_watch_alias(me: Me, alias_id: str | None) -> str | None:
        """选择明确指定的地址，缺省时选择主地址。"""
        if alias_id is None:
            selected = next((alias for alias in me.aliases if alias.is_primary), None)
        else:
            selected = next(
                (alias for alias in me.aliases if alias.alias_id == alias_id),
                None,
            )
        return selected.alias_id if selected is not None else None

    async def _start_watch(
        self,
        umo: str,
        account: str,
        alias_id: str | None = None,
    ) -> str | None:
        """校验账号地址并启动持久化监听，返回错误文本或 None。"""
        logger.debug(
            f"[agently] 监听启动请求：账号={account}，地址ID={alias_id}"
        )
        try:
            bundle = await self._token_store.load(account)
        except TokenStoreError as exc:
            logger.warning(
                f"[agently] 监听启动读取凭证失败：账号={account}，异常={type(exc).__name__}"
            )
            return f"读取账号 {account} 凭证失败：{exc}"
        if bundle is None:
            logger.warning(f"[agently] 监听启动失败：账号={account}，原因=凭证不存在")
            return f"账号 {account} 没有已保存的凭证，请先登录。"

        client, _ = self._clients_for(account)
        try:
            me = await client.me()
        except (ClientError, OAuthError, TokenStoreError) as exc:
            logger.warning(
                f"[agently] 监听启动读取地址失败：账号={account}，异常={type(exc).__name__}"
            )
            return f"读取账号 {account} 的邮箱地址失败：{exc}"

        selected_alias = self._select_watch_alias(me, alias_id)
        if selected_alias is None:
            logger.warning(
                f"[agently] 监听启动地址解析失败：账号={account}，地址ID={alias_id}"
            )
            if alias_id is None:
                return "/v1/me 未返回 is_primary 别名"
            return "邮箱地址不存在或不属于该账号"

        logger.debug(
            f"[agently] 监听地址解析完成：账号={account}，地址ID={selected_alias}"
        )
        async with self._watch_config_lock:
            entry = self._watch_tasks.get(umo)
            if entry is not None:
                if not entry.task.done():
                    return f"当前会话已在监听账号 {entry.account} 的新邮件。"
                self._watch_tasks.pop(umo, None)

            try:
                configs = await self._read_watch_configs_locked()
            except Exception as exc:
                logger.exception(f"读取会话 {umo} 的监听配置失败")
                return f"启动新邮件监听失败：读取持久化配置失败：{exc}"
            if configs is None:
                return "启动新邮件监听失败：持久化配置根数据损坏，未覆盖原数据。"

            next_cursor = None
            seen_message_ids: list[str] = []
            if umo in configs:
                current = self._validate_watch_config(umo, configs[umo])
                if current is None:
                    return "当前会话的监听配置损坏，请先停止监听清理后重试。"
                if current["account"] != account or current["alias_id"] != selected_alias:
                    return "当前会话已有其他账号或地址的监听配置，请先执行 watch_stop。"
                next_cursor = current["next_cursor"]
                seen_message_ids = current["seen_message_ids"]
                logger.debug(
                    f"[agently] 监听恢复持久化进度：账号={account}，地址ID={selected_alias}，"
                    f"游标存在={next_cursor is not None}，去重数量={len(seen_message_ids)}"
                )

            watch_id = self._new_watch_id()
            configs[umo] = {
                "watch_id": watch_id,
                "account": account,
                "alias_id": selected_alias,
                "next_cursor": next_cursor,
                "seen_message_ids": seen_message_ids,
            }
            try:
                await self._write_watch_configs_locked(configs)
            except Exception as exc:
                logger.exception(f"保存会话 {umo} 的监听配置失败")
                return f"启动新邮件监听失败：保存持久化配置失败：{exc}"

            async def save_state(
                cursor: str | None,
                seen_ids: list[str],
            ) -> None:
                await self._save_watch_state(
                    umo,
                    watch_id,
                    account,
                    selected_alias,
                    cursor,
                    seen_ids,
                )

            try:
                watcher = MessageWatcher(
                    client,
                    alias_id=selected_alias,
                    next_cursor=next_cursor,
                    seen_message_ids=seen_message_ids,
                    save_state=save_state,
                )
                task = asyncio.create_task(
                    self._watch_loop(umo, account, selected_alias, watch_id, watcher)
                )
            except Exception as exc:
                cleanup_error = None
                try:
                    current = configs.get(umo)
                    if isinstance(current, dict) and current.get("watch_id") == watch_id:
                        configs.pop(umo, None)
                        await self._write_watch_configs_locked(configs)
                except Exception as cleanup_exc:
                    cleanup_error = cleanup_exc
                logger.exception(f"创建会话 {umo} 的新邮件监听任务失败")
                if cleanup_error is not None:
                    return (
                        f"创建新邮件监听任务失败：{exc}；"
                        f"清理持久化配置也失败：{cleanup_error}"
                    )
                return f"创建新邮件监听任务失败：{exc}"

            self._watch_tasks[umo] = _WatchTaskEntry(
                task=task,
                account=account,
                alias_id=selected_alias,
                watch_id=watch_id,
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            task.add_done_callback(
                lambda finished, _umo=umo, _task=task, _watch_id=watch_id: (
                    self._discard_watch(_umo, _task, _watch_id)
                )
            )
        logger.info(
            f"[agently] 新邮件监听已启动：账号={account}，地址ID={selected_alias}，"
            f"游标存在={next_cursor is not None}，去重数量={len(seen_message_ids)}"
        )
        return None

    async def _restore_watches(self) -> None:
        """恢复仍有会话绑定的监听配置，逐条记录失败原因。"""
        async with self._watch_config_lock:
            configs = await self._read_watch_configs_locked()
        if configs is None:
            logger.warning("[agently] 监听恢复跳过：持久化配置根数据损坏")
            return

        restored = 0
        failed = 0
        skipped = 0
        bindings = await self._load_bindings()
        logger.debug(f"[agently] 监听恢复开始：配置数量={len(configs)}")
        for umo, raw_config in configs.items():
            config = self._validate_watch_config(umo, raw_config)
            if config is None:
                skipped += 1
                continue
            bound_account = bindings.get(umo)
            if not isinstance(bound_account, str) or not bound_account:
                skipped += 1
                logger.warning(
                    f"会话 {umo} 没有账号绑定，保留账号 {config['account']} 的监听配置"
                )
                continue
            try:
                error = await self._start_watch(
                    umo,
                    config["account"],
                    config["alias_id"],
                )
            except Exception:
                failed += 1
                logger.exception(f"恢复会话 {umo} 的新邮件监听失败，配置已保留")
                continue
            if error is not None:
                failed += 1
                logger.warning(f"恢复会话 {umo} 的新邮件监听失败：{error}；配置已保留")
                continue
            restored += 1
        logger.info(
            f"[agently] 监听恢复完成：配置数量={len(configs)}，恢复数量={restored}，"
            f"失败数量={failed}，跳过数量={skipped}"
        )

    async def _stop_watch(self, umo: str) -> tuple[bool, str | None]:
        """取消任务并删除持久化配置，返回是否发现监听及清理错误。"""
        task = None
        found = False
        error = None

        logger.debug("[agently] 监听停止请求")
        async with self._watch_config_lock:
            entry = self._watch_tasks.pop(umo, None)
            if entry is not None:
                found = True
                task = entry.task
                task.cancel()
                logger.debug(
                    f"[agently] 监听任务已请求取消：账号={entry.account}，地址ID={entry.alias_id}"
                )

            try:
                configs = await self._read_watch_configs_locked()
            except Exception as exc:
                logger.exception(f"读取会话 {umo} 的监听配置失败")
                error = f"停止新邮件监听失败：读取持久化配置失败：{exc}"
            else:
                if configs is None:
                    error = "停止新邮件监听失败：持久化配置根数据损坏，未修改原数据。"
                elif umo in configs:
                    found = True
                    configs.pop(umo, None)
                    try:
                        await self._write_watch_configs_locked(configs)
                    except Exception as exc:
                        logger.exception(f"删除会话 {umo} 的监听配置失败")
                        error = f"停止新邮件监听失败：清理持久化配置失败：{exc}"

        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        if found and error is None:
            logger.info("[agently] 新邮件监听已停止")
        else:
            logger.debug(f"[agently] 监听停止结束：发现监听={found}，存在错误={error is not None}")
        return found, error

    def _discard_watch(self, umo: str, task: asyncio.Task, watch_id: str) -> None:
        """只清理匹配会话、任务和监听代次的登记。"""
        entry = self._watch_tasks.get(umo)
        if (
            entry is not None
            and entry.task is task
            and entry.watch_id == watch_id
        ):
            del self._watch_tasks[umo]
            logger.debug("[agently] 监听任务登记已清理")

    async def _watch_loop(
        self,
        umo: str,
        account: str,
        alias_id: str,
        watch_id: str,
        watcher: MessageWatcher,
    ) -> None:
        """持续推送新邮件，网络或凭证失败时退避重连。"""
        reconnect_delay = 5.0
        logger.debug(
            f"[agently] 监听循环开始：账号={account}，地址ID={alias_id}，代次={watch_id}"
        )
        try:
            while True:
                try:
                    async for message in watcher.watch():
                        logger.debug(
                            f"[agently] 新邮件事件推送开始：账号={account}，地址ID={alias_id}，"
                            f"邮件ID={message.message_id}"
                        )
                        pushed = await self._push(umo, format_new_mail_notice(message))
                        if not pushed:
                            raise RuntimeError("新邮件推送失败，当前事件尚未确认")
                        logger.debug(
                            f"[agently] 新邮件事件推送完成：账号={account}，"
                            f"邮件ID={message.message_id}"
                        )
                        reconnect_delay = 5.0
                except (ClientError, OAuthError, TokenStoreError) as exc:
                    logger.warning(
                        f"[agently] 监听连接失败，准备重连：账号={account}，地址ID={alias_id}，"
                        f"代次={watch_id}，异常={type(exc).__name__}，"
                        f"代码={getattr(exc, 'code', None)}，"
                        f"状态码={getattr(exc, 'status_code', None)}，"
                        f"重连延迟={reconnect_delay:g}秒"
                    )
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 60.0)
                    logger.debug(
                        f"[agently] 监听重连退避更新：账号={account}，"
                        f"下次延迟={reconnect_delay:g}秒"
                    )
        except asyncio.CancelledError:
            logger.debug(
                f"[agently] 监听循环已取消：账号={account}，地址ID={alias_id}，代次={watch_id}"
            )
            raise
        except Exception as exc:
            logger.exception(
                f"[agently] 监听循环异常停止：账号={account}，地址ID={alias_id}，代次={watch_id}"
            )
            await self._push(
                umo,
                f"账号 {account} 地址 {alias_id} 的新邮件监听已停止：{exc}",
            )

    async def _record_send(self, account: str) -> None:
        """把账号的本地发送计数加一，跨日自动重置。"""
        async with self._send_counter_lock:
            counters = await self.get_kv_data(SEND_COUNTER_KEY, {}) or {}
            today = time.strftime("%Y-%m-%d")
            entry = counters.get(account)
            if isinstance(entry, dict) and entry.get("date") == today:
                previous = entry.get("count")
                if isinstance(previous, int) and not isinstance(previous, bool) and previous >= 0:
                    count = previous + 1
                else:
                    count = 1
            else:
                count = 1
            counters[account] = {"date": today, "count": count}
            logger.debug(
                f"[agently] 发送计数保存开始：账号={account}，日期={today}，计数={count}"
            )
            await self.put_kv_data(SEND_COUNTER_KEY, counters)
            logger.debug(
                f"[agently] 发送计数保存完成：账号={account}，日期={today}，计数={count}"
            )

    async def _send_count_today(self, account: str) -> int:
        """读取账号今日的本地发送计数。"""
        async with self._send_counter_lock:
            counters = await self.get_kv_data(SEND_COUNTER_KEY, {}) or {}
            entry = counters.get(account)
            today = time.strftime("%Y-%m-%d")
            if not isinstance(entry, dict) or entry.get("date") != today:
                count = 0
            else:
                raw_count = entry.get("count")
                count = (
                    raw_count
                    if isinstance(raw_count, int)
                    and not isinstance(raw_count, bool)
                    and raw_count >= 0
                    else 0
                )
            logger.debug(
                f"[agently] 发送计数读取完成：账号={account}，日期={today}，计数={count}"
            )
            return count

    @staticmethod
    def _primary_email(me: Me | None) -> str:
        """取身份中的主别名邮箱地址，me 为空或取不到时降级为占位文本。"""
        if me is None:
            return "未知（未缓存）"
        primary = next((alias for alias in me.aliases if alias.is_primary), None)
        if primary is None:
            return "未知"
        if primary.email:
            return primary.email
        return primary.alias_id

    async def _load_bindings(self) -> dict[str, str]:
        """读取会话到账号的绑定表。"""
        bindings = await self.get_kv_data(BINDINGS_KEY, {}) or {}
        logger.debug(f"[agently] 绑定表读取完成：键={BINDINGS_KEY}，数量={len(bindings)}")
        return bindings

    async def _save_bindings(self, bindings: dict[str, str]) -> None:
        """整体写回会话到账号的绑定表。"""
        logger.debug(f"[agently] 绑定表保存开始：键={BINDINGS_KEY}，数量={len(bindings)}")
        await self.put_kv_data(BINDINGS_KEY, bindings)
        logger.debug(f"[agently] 绑定表保存完成：键={BINDINGS_KEY}，数量={len(bindings)}")

    async def _push(self, umo: str, text: str) -> bool:
        """向指定会话主动推送文本，并返回是否发送成功。"""
        logger.debug(f"[agently] 会话推送开始：会话={umo}")
        try:
            await self.context.send_message(umo, MessageChain().message(text))
        except Exception:
            logger.exception(f"向会话 {umo} 推送消息失败")
            return False
        logger.debug(f"[agently] 会话推送完成：会话={umo}")
        return True
