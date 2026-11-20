"""实现 QQ Agently Mail 的登录会话承载、账号绑定指令与 LLM 工具面。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time

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
from .agentlymail.watcher import MessageWatcher
from .agentlymail.web_api import register_web_apis

# 设备流 expires_in 固定为 600 秒，作为登录防重入的过期上限。
LOGIN_FLOW_TTL_SECONDS = 600.0

# KV 中会话到账号的绑定表键名，值为 {unified_msg_origin: 账号名}。
BINDINGS_KEY = "umo_bindings"

# KV 中本地发送计数的键名，值为 {账号名: {date: "YYYY-MM-DD", count: N}}。
SEND_COUNTER_KEY = "send_counter"


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
        self._watch_tasks: dict[str, tuple[asyncio.Task, str]] = {}
        self._send_counter_lock = asyncio.Lock()
        self._web_api = None

    async def initialize(self):
        """注册 LLM 工具与 Web API。"""
        if self._config.get("tools_enabled", True):
            self.context.add_llm_tools(*make_tools(self))
        self._web_api = register_web_apis(self)

    async def terminate(self):
        """插件卸载时取消全部后台任务并关闭共享 HTTP 客户端。"""
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._web_api is not None:
            await self._web_api.cleanup()
        await self._auth_http.aclose()
        await self._api_http.aclose()

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
        deleted, unbound = await self._remove_account(account)
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
    async def bind(self, event: AstrMessageEvent, account: str):
        """把当前会话绑定到指定账号"""
        if await self._token_store.load(account) is None:
            await event.send(
                event.plain_result(
                    f"账号 {account} 没有已保存的凭证，请先执行 /agently login {account}。",
                ),
            )
            return
        bindings = await self._load_bindings()
        previous = bindings.get(event.unified_msg_origin)
        if previous is not None and previous != account:
            self._stop_watch(event.unified_msg_origin)
        bindings[event.unified_msg_origin] = account
        await self._save_bindings(bindings)
        await event.send(event.plain_result(f"已把当前会话绑定到账号 {account}。"))

    @agently_group.command("unbind")
    async def unbind(self, event: AstrMessageEvent):
        """解除当前会话的账号绑定"""
        bindings = await self._load_bindings()
        umo = event.unified_msg_origin
        account = bindings.pop(umo, None)
        if account is None:
            self._stop_watch(umo)
            await event.send(event.plain_result("当前会话未绑定账号。"))
            return
        self._stop_watch(umo)
        await self._save_bindings(bindings)
        await event.send(event.plain_result(f"已解除当前会话与账号 {account} 的绑定。"))

    def _clients_for(self, account: str) -> tuple[AgentlyMailClient, OAuthClient]:
        """返回账号的客户端对，按需创建并缓存。"""
        pair = self._clients.get(account)
        if pair is None:
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
        if not await self._registry.start(umo, LOGIN_FLOW_TTL_SECONDS):
            return "该会话已有登录在途，请等待结果推送或过期后再试。"
        _, oauth = self._clients_for(account)
        try:
            flow = await oauth.start()
        except OAuthError as exc:
            await self._registry.finish(umo)
            return f"登录发起失败：{exc.message}"
        except Exception as exc:
            await self._registry.finish(umo)
            logger.exception(f"账号 {account} 发起设备流失败")
            return f"登录发起失败：{exc}"

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
                f"账号 {account} 无头二维码创建失败，降级发送浏览器链接：{exc}"
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
                    f"账号 {account} 二维码发送失败，改发浏览器链接：{exc}"
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
        except HeadlessQRLoginError as exc:
            await self._push(
                umo,
                f"账号 {account} 登录失败：{self._headless_error_message(exc)}",
            )
        except AccessDeniedError as exc:
            await self._push(umo, f"账号 {account} 登录失败：{exc.message}。")
        except ExpiredTokenError as exc:
            await self._push(
                umo,
                f"账号 {account} 登录失败：{exc.message}，"
                "二维码有效期约 2-3 分钟，请重新执行 /agently login。",
            )
        except AuthorizationPendingError as exc:
            await self._push(
                umo,
                f"账号 {account} 登录超时：{exc.message}，请重新执行 /agently login。",
            )
        except OAuthError as exc:
            await self._push(umo, f"账号 {account} 登录失败：{exc.message}。")
        except Exception as exc:
            logger.exception(f"账号 {account} 的设备流轮询异常")
            await self._push(
                umo,
                f"账号 {account} 登录失败：发生未预期的错误（{exc}），详情见插件日志。",
            )
        else:
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
        # 凭证已在 poll 内保存，先绑定会话；绑定不依赖 /v1/me 是否成功。
        bindings = await self._load_bindings()
        bindings[umo] = account
        await self._save_bindings(bindings)
        try:
            me = await client.me()
        except ClientError as exc:
            logger.warning(f"账号 {account} 登录成功但读取邮箱信息失败: {exc}")
            await self._push(
                umo,
                f"账号 {account} 登录成功：app_id {bundle.app_id or '未知'}，"
                f"但读取邮箱信息失败：{exc}\n"
                f"已自动把当前会话绑定到账号 {account}。",
            )
            return
        self._me_cache[account] = me
        limits = json.dumps(me.rate_limits.values, ensure_ascii=False)
        email = self._primary_email(me)
        await self._push(
            umo,
            f"账号 {account} 登录成功：邮箱 {email}（app_id {bundle.app_id or '未知'}）\n"
            f"限额 {limits}\n"
            f"已自动把当前会话绑定到账号 {account}。",
        )

    def _stop_watches_for_account(self, account: str) -> None:
        """取消账号对应的全部会话监听，防止解绑或登出后继续推送。"""
        for umo, (task, watched_account) in list(self._watch_tasks.items()):
            if watched_account == account:
                self._watch_tasks.pop(umo, None)
                task.cancel()

    async def _remove_account(self, account: str) -> tuple[bool, int]:
        """删除账号凭证并级联解绑引用它的会话，返回 (是否删除, 解绑会话数)。"""
        if not await self._token_store.delete(account):
            return False, 0
        self._stop_watches_for_account(account)
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
        return True, len(referenced)

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

    async def _start_watch(self, umo: str, account: str) -> str | None:
        """为会话启动新邮件监听，返回错误文本或 None。"""
        if umo in self._watch_tasks:
            return f"当前会话已在监听账号 {self._watch_tasks[umo][1]} 的新邮件。"
        if await self._token_store.load(account) is None:
            return f"账号 {account} 没有已保存的凭证，请先登录。"
        client, _ = self._clients_for(account)
        task = asyncio.create_task(
            self._watch_loop(umo, account, MessageWatcher(client)),
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(
            lambda finished, _umo=umo, _task=task: self._discard_watch(_umo, _task)
        )
        self._watch_tasks[umo] = (task, account)
        return None

    def _stop_watch(self, umo: str) -> str | None:
        """停止会话的新邮件监听，返回错误文本或 None。"""
        entry = self._watch_tasks.pop(umo, None)
        if entry is None:
            return "当前会话没有在监听新邮件。"
        entry[0].cancel()
        return None

    def _discard_watch(self, umo: str, task: asyncio.Task) -> None:
        """监听任务结束后清理登记；该会话已换新任务时不动。"""
        entry = self._watch_tasks.get(umo)
        if entry is not None and entry[0] is task:
            del self._watch_tasks[umo]

    async def _watch_loop(
        self,
        umo: str,
        account: str,
        watcher: MessageWatcher,
    ) -> None:
        """循环等待新邮件并推送到会话，异常退出时说明原因。"""
        try:
            async for message in watcher.watch():
                await self._push(umo, format_new_mail_notice(message))
        except Exception as exc:
            logger.warning(f"账号 {account} 的新邮件监听退出: {exc}")
            await self._push(umo, f"账号 {account} 的新邮件监听已停止：{exc}")

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
            await self.put_kv_data(SEND_COUNTER_KEY, counters)

    async def _send_count_today(self, account: str) -> int:
        """读取账号今日的本地发送计数。"""
        async with self._send_counter_lock:
            counters = await self.get_kv_data(SEND_COUNTER_KEY, {}) or {}
            entry = counters.get(account)
            if not isinstance(entry, dict) or entry.get("date") != time.strftime("%Y-%m-%d"):
                return 0
            count = entry.get("count")
            return count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0

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
        return await self.get_kv_data(BINDINGS_KEY, {}) or {}

    async def _save_bindings(self, bindings: dict[str, str]) -> None:
        """整体写回会话到账号的绑定表。"""
        await self.put_kv_data(BINDINGS_KEY, bindings)

    async def _push(self, umo: str, text: str) -> None:
        """向指定会话主动推送一条文本消息，失败只记日志。"""
        try:
            await self.context.send_message(umo, MessageChain().message(text))
        except Exception:
            logger.exception(f"向会话 {umo} 推送消息失败")
