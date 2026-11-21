"""实现 QQ Agently Mail 的登录会话承载、账号管理指令与 LLM 工具面。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4
import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
try:
    from astrbot.api.util import SessionController, SessionFilter, session_waiter
except ImportError:
    # 旧版 AstrBot 未从公共路径导出 SessionFilter，回退到其定义所在模块。
    from astrbot.api.util import SessionController, session_waiter
    from astrbot.core.utils.session_waiter import SessionFilter

from .agentlymail.client import AgentlyMailClient, ClientError
from .agentlymail.constants import API_BASE, AUTH_BASE
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


class _LoginCancelFilter(SessionFilter):
    """只匹配原会话、原发起人且文本为取消指令的消息，其余消息一律返回不匹配。"""

    def __init__(self, anchor: AstrMessageEvent):
        self._umo = anchor.unified_msg_origin
        self._sender_id = anchor.get_sender_id()
        self._key = f"agently-login-cancel|{self._umo}|{self._sender_id}"
        # 注册阶段拿到的是登录事件本身，不是取消文本，用对象标识让它返回会话键。
        self._anchor = anchor
        self._miss = f"agently-login-cancel-miss|{uuid4().hex}"

    def filter(self, event: AstrMessageEvent) -> str:
        if (
            event.unified_msg_origin != self._umo
            or event.get_sender_id() != self._sender_id
        ):
            return self._miss
        if event is self._anchor:
            return self._key
        if event.message_str.strip().lower() in {"取消", "c", "cancel"}:
            return self._key
        return self._miss


class LoginCancelledError(Exception):
    """表示发起人在等待授权期间通过会话回复取消了登录。"""

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

    @staticmethod
    def _validate_account_name(account: str) -> str | None:
        """校验账号名合法性，拒绝空串、超长值与路径特殊字符。"""
        if not isinstance(account, str):
            return None
        name = account.strip()
        if (
            not name
            or len(name) > 64
            or name in {".", ".."}
            or any(char in name for char in ("\x00", "/", "\\"))
        ):
            return None
        return name

    def _umo_filter_config(self) -> tuple[str, set[str]]:
        """读取并规范化 UMO 黑/白名单配置，非法模式回退为 whitelist。"""
        raw_mode = self._config.get("umo_filter_mode", "whitelist")
        mode = str(raw_mode or "whitelist").strip().lower()
        if mode not in {"whitelist", "blacklist"}:
            logger.warning(f"[agently] UMO 过滤模式无效，已回退为白名单：配置值={raw_mode!r}")
            mode = "whitelist"
        raw_list = self._config.get("umo_filter_list", []) or []
        umos = (
            {item.strip() for item in raw_list if isinstance(item, str) and item.strip()}
            if isinstance(raw_list, list)
            else set()
        )
        return mode, umos

    @agently_group.command("login")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def login(self, event: AstrMessageEvent, account: str = "default"):
        """发起扫码登录，可带账号名，缺省 default"""
        valid = self._validate_account_name(account)
        if valid is None:
            await event.send(event.plain_result("账号名无效：不能为空、超过 64 字符或包含非法路径字符。"))
            return
        error = await self._start_login(event, valid)
        if error is not None:
            await event.send(event.plain_result(error))

    @agently_group.command("logout")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def logout(self, event: AstrMessageEvent, account: str = "default"):
        """删除账号凭证并清理其监听，可带账号名，缺省 default"""
        valid = self._validate_account_name(account)
        if valid is None:
            await event.send(event.plain_result("账号名无效：不能为空、超过 64 字符或包含非法路径字符。"))
            return
        deleted, error = await self._remove_account(valid)
        if error is not None:
            await event.send(event.plain_result(error))
            return
        if not deleted:
            await event.send(event.plain_result(f"账号 {valid} 没有已保存的凭证。"))
            return
        await event.send(
            event.plain_result(f"已删除账号 {valid} 的凭证。"),
        )

    @agently_group.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def status(self, event: AstrMessageEvent):
        """列出全部已登录账号与当前会话的访问控制状态"""
        snapshot = await self._account_snapshot(event.unified_msg_origin)
        lines = []
        for item in snapshot["accounts"]:
            if "invalid" in item:
                lines.append(f"- {item['account']}：凭证文件无效（{item['invalid']}）")
                continue
            mark = "（默认账号）" if item["account"] == "default" else ""
            lines.append(
                f"- {item['account']}{mark}：邮箱 {item['email']}，"
                f"凭证有效期至 {item['expires_at']}，app_id {item['app_id'] or '未知'}",
            )
        parts = []
        if lines:
            parts.append("已保存凭证的账号：\n" + "\n".join(lines))
        else:
            parts.append("还没有任何账号凭证，请在 WebUI「邮局」扫码或执行 /agently login。")
        mode_label = "白名单" if snapshot["umo_filter_mode"] == "whitelist" else "黑名单"
        in_list_label = "在" if snapshot["umo_in_list"] else "不在"
        parts.append(
            f"会话访问控制：{mode_label}模式（列表共 {snapshot['umo_filter_count']} 个会话，"
            f"当前会话{in_list_label}列表中）。"
        )
        if snapshot["login_in_flight"]:
            parts.append("当前会话有登录在途，请等待结果推送。")
        await event.send(event.plain_result("\n".join(parts)))

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
        valid = self._validate_account_name(account)
        if valid is None:
            return "账号名无效：不能为空、超过 64 字符或包含非法路径字符。"
        account = valid
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
        notice = (
            f"账号 {account} 登录发起成功，请复制下一条授权链接在浏览器中打开，扫码并在手机上点确定。\n"
            "等待授权期间可直接回复「取消」终止本次登录；授权链接约 10 分钟过期，"
            "过期后请重新执行登录；完成后会自动推送结果。"
        )
        try:
            await event.send(MessageChain().message(notice))
        except Exception as exc:
            await self._registry.finish(umo)
            logger.exception(f"账号 {account} 的登录通知发送失败")
            return f"登录发起成功，但通知发送失败：{exc}"

        try:
            await event.send(MessageChain().message(flow.browser_url))
        except Exception as exc:
            await self._registry.finish(umo)
            logger.exception(f"账号 {account} 的授权链接发送失败")
            return f"登录发起成功，但授权链接发送失败：{exc}"

        task = asyncio.create_task(
            self._complete_device_flow(
                event,
                account,
                flow,
                deadline=deadline,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info(f"[agently] 登录通知已发送并启动后台授权：账号={account}")
        return None


    async def _watch_login_cancel(
        self, event: AstrMessageEvent, deadline: float
    ) -> bool | None:
        """等待发起人回复「取消」；返回 True 表示用户取消，监听器到期返回 None 交给 poll 自身超时。"""
        session_filter = _LoginCancelFilter(event)
        remaining = max(0.1, deadline - time.monotonic())

        @session_waiter(timeout=remaining)
        async def cancel_waiter(
            controller: SessionController,
            incoming: AstrMessageEvent,
        ) -> None:
            controller.stop()

        try:
            await cancel_waiter(event, session_filter=session_filter)
        except TimeoutError:
            return None
        return True

    async def _complete_device_flow(
        self,
        event: AstrMessageEvent,
        account: str,
        flow: DeviceFlowStart,
        *,
        deadline: float,
    ) -> None:
        """后台完成设备流授权，等待期间发起人可回复「取消」终止，完成后回推结果。"""
        umo = event.unified_msg_origin
        client, oauth = self._clients_for(account)
        logger.debug(f"[agently] 后台授权开始：账号={account}")
        poll_task = asyncio.create_task(oauth.poll(flow, deadline=deadline))
        cancel_task = asyncio.create_task(self._watch_login_cancel(event, deadline))
        try:
            done, _pending = await asyncio.wait(
                {poll_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # 仅发起人回复「取消」才终止；取消监听器自身到期不算取消，交给 poll 决定到期结果。
            if cancel_task in done and cancel_task.result() is True:
                raise LoginCancelledError()
            bundle = await poll_task
        except asyncio.CancelledError:
            logger.debug(f"[agently] 后台授权已取消：账号={account}")
            raise
        except LoginCancelledError:
            logger.info(f"[agently] 登录已被发起人取消：账号={account}")
            await self._push(umo, f"账号 {account} 登录已取消。")
        except AccessDeniedError as exc:
            logger.warning(f"[agently] 用户拒绝设备授权：账号={account}")
            await self._push(umo, f"账号 {account} 登录失败：{exc.message}。")
        except ExpiredTokenError as exc:
            logger.warning(f"[agently] 设备授权已过期：账号={account}")
            await self._push(
                umo,
                f"账号 {account} 登录失败：{exc.message}，"
                "授权链接约 10 分钟有效，请重新执行 /agently login。",
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
            logger.info(f"[agently] 登录授权完成，开始收尾回推：账号={account}")
            try:
                await self._announce_login_success(umo, account, bundle, client)
            except Exception:
                logger.exception(f"账号 {account} 登录成功后的收尾回推失败")
                await self._push(
                    umo,
                    f"账号 {account} 登录成功，但收尾步骤出错，"
                    "请执行 /agently status 检查。",
                )
        finally:
            logger.debug(f"[agently] 后台授权进入清理：账号={account}")
            for pending_task in (poll_task, cancel_task):
                if not pending_task.done():
                    pending_task.cancel()
            await asyncio.gather(poll_task, cancel_task, return_exceptions=True)
            # 轮询与取消监听都清理完再解除在途登记，避免影响同会话的下一次登录。
            await self._registry.finish(umo)

    async def _announce_login_success(
        self,
        umo: str,
        account: str,
        bundle: TokenBundle,
        client: AgentlyMailClient,
    ) -> None:
        """登录成功后读取邮箱信息并回推。"""
        logger.info(f"[agently] 登录授权完成：账号={account}")
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
                f"但读取邮箱信息失败：{exc}",
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
            f"限额 {limits}",
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

    async def _remove_account(self, account: str) -> tuple[bool, str | None]:
        """先清理监听，再删除凭证，并返回明确清理错误。"""
        valid = self._validate_account_name(account)
        if valid is None:
            return False, "账号名无效：不能为空、超过 64 字符或包含非法路径字符。"
        account = valid
        logger.debug(f"[agently] 账号登出开始：账号={account}")
        cleanup_errors = await self._stop_watches_for_account(account)
        if cleanup_errors:
            return (
                False,
                f"账号 {account} 登出失败：{'；'.join(cleanup_errors)}",
            )

        logger.debug(f"[agently] 账号监听清理完成：账号={account}")
        if not await self._token_store.delete(account):
            return False, None
        self._clients.pop(account, None)
        self._me_cache.pop(account, None)
        logger.info(f"[agently] 账号登出完成：账号={account}")
        return True, None

    async def _account_snapshot(self, umo: str) -> dict:
        """枚举账号凭证与当前会话的访问控制状态，供命令与工具共用。"""
        accounts = await self._token_store.list_accounts()
        mode, umos = self._umo_filter_config()
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
            "default_account": "default",
            "umo_filter_mode": mode,
            "umo_filter_count": len(umos),
            "umo_in_list": umo in umos,
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
        """恢复已持久化的监听配置，黑名单会话跳过，逐条记录失败原因。"""
        async with self._watch_config_lock:
            configs = await self._read_watch_configs_locked()
        if configs is None:
            logger.warning("[agently] 监听恢复跳过：持久化配置根数据损坏")
            return

        restored = 0
        failed = 0
        skipped = 0
        mode, umos = self._umo_filter_config()
        logger.debug(f"[agently] 监听恢复开始：配置数量={len(configs)}")
        for umo, raw_config in configs.items():
            config = self._validate_watch_config(umo, raw_config)
            if config is None:
                skipped += 1
                continue
            if mode == "blacklist" and umo in umos:
                skipped += 1
                logger.warning(
                    f"会话 {umo} 在黑名单中，跳过恢复账号 {config['account']} 的监听配置"
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

    async def _watched_account_for(self, umo: str) -> str | None:
        """返回会话当前运行中或已持久化监听的账号名，无监听时返回 None。"""
        entry = self._watch_tasks.get(umo)
        if entry is not None and not entry.task.done():
            return entry.account
        async with self._watch_config_lock:
            try:
                configs = await self._read_watch_configs_locked()
            except Exception:
                return None
            if not configs or umo not in configs:
                return None
            raw = configs.get(umo)
            if isinstance(raw, dict):
                acct = raw.get("account")
                if isinstance(acct, str) and acct.strip():
                    return acct.strip()
        return None

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
