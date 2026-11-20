"""定义插件暴露给 LLM 的三个 FunctionTool 与守卫、结果、schema 助手。"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import json
import mimetypes
import time

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .client import ClientError
from .oauth import OAuthError
from .render import format_message, format_summary_list
from .token_store import TokenStoreError

if TYPE_CHECKING:
    from ..main import QQAgentlyMailPlugin

# 附件上限取自 artifacts/api-map.md 的运行时约束观测值，服务器仍是最终权威。
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENT_COUNT = 50
MAX_TOTAL_ATTACHMENT_BYTES = 20 * 1024 * 1024

# read 读取正文时的缺省截断长度。
DEFAULT_MAX_BODY_CHARS = 8000

def _safe_path_identifier(value: str) -> bool:
    """判断协议标识是否可以安全地出现在 URL 和附件目录名中。"""
    return bool(value) and value not in {".", ".."} and all(
        ord(char) >= 32 and char not in "/\\?#%" for char in value
    )


def ok_result(data: Any) -> ToolExecResult:
    """构造成功结果 JSON 文本。"""
    return json.dumps({"ok": True, "data": data}, ensure_ascii=False)


def error_result(error: str, hint: str | None = None) -> ToolExecResult:
    """构造失败结果 JSON 文本，中文错误与可选提示。"""
    payload: dict[str, Any] = {"ok": False, "error": error}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False)


_UNSET = object()


def string_param(description: str, default: Any = _UNSET) -> dict[str, Any]:
    parameter = {"type": "string", "description": description}
    if default is not _UNSET:
        parameter["default"] = default
    return parameter


def integer_param(description: str, default: Any = _UNSET) -> dict[str, Any]:
    parameter = {"type": "integer", "description": description}
    if default is not _UNSET:
        parameter["default"] = default
    return parameter


def boolean_param(description: str, default: Any = _UNSET) -> dict[str, Any]:
    parameter = {"type": "boolean", "description": description}
    if default is not _UNSET:
        parameter["default"] = default
    return parameter


def string_array_param(description: str) -> dict[str, Any]:
    return {"type": "array", "description": description, "items": {"type": "string"}}


def enum_param(
    description: str,
    values: list[str],
    default: Any = _UNSET,
) -> dict[str, Any]:
    parameter: dict[str, Any] = {
        "type": "string",
        "description": f"{description}（{'/'.join(values)}）",
        "enum": values,
    }
    if default is not _UNSET:
        parameter["default"] = default
    return parameter


def object_parameters(
    properties: dict[str, dict[str, Any]],
    *,
    required: list[str],
) -> dict[str, Any]:
    """组装 JSON Schema 对象参数，键集合与各适配器的子集保持一致。"""
    for name in required:
        if name not in properties:
            raise ValueError(f"required 引用了未声明的参数: {name}")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _admin_only_error(
    context: ContextWrapper[AstrAgentContext],
) -> ToolExecResult | None:
    """管理员守卫：事件缺失或非管理员时返回权限错误 JSON。"""
    event = getattr(context.context, "event", None)
    if event is not None and event.is_admin():
        return None
    return error_result("权限不足", "QQ Agently Mail 管理工具仅管理员可用。")


async def _resolve_account(
    plugin: QQAgentlyMailPlugin,
    event: AstrMessageEvent,
) -> tuple[str | None, ToolExecResult | None]:
    """按会话绑定解析账号，未绑定时返回提示先绑定的错误结果。"""
    bindings = await plugin._load_bindings()
    account = bindings.get(event.unified_msg_origin)
    if account is None:
        logger.warning("[agently] 工具账号解析失败：当前会话未绑定账号")
        return None, error_result(
            "当前会话未绑定账号",
            "请先让管理员执行 /agently bind <账号名> 绑定已有账号，或执行 /agently login。",
        )
    logger.debug(f"[agently] 工具账号解析完成：账号={account}")
    return account, None


@dataclass(frozen=True)
class _ResolvedMailbox:
    """保存一次工具调用选定的账号、客户端和邮箱地址。"""

    account: str
    client: Any
    me: Any
    alias_id: str
    email: str


async def _resolve_mailbox(
    plugin: QQAgentlyMailPlugin,
    event: AstrMessageEvent,
    kwargs: dict[str, Any],
) -> tuple[_ResolvedMailbox | None, ToolExecResult | None]:
    """解析账号和别名，并在邮件操作前校验跨账号权限、凭证与别名归属。"""
    raw_account = kwargs.get("account")
    if "account" in kwargs and raw_account is not None:
        if not isinstance(raw_account, str) or not raw_account.strip():
            logger.warning("[agently] 工具邮箱解析失败：账号不是非空字符串")
            return None, error_result("account 必须是非空字符串")
        account = raw_account.strip()
        if account in {".", ".."} or any(
            char in account for char in ("\x00", "/", "\\")
        ):
            logger.warning("[agently] 工具邮箱解析失败：账号含非法路径字符")
            return None, error_result("account 为空或含非法路径字符")

        bindings = await plugin._load_bindings()
        raw_bound = bindings.get(event.unified_msg_origin)
        bound_account = (
            raw_bound.strip()
            if isinstance(raw_bound, str) and raw_bound.strip()
            else None
        )
        if account != bound_account and not event.is_admin():
            logger.warning(
                f"[agently] 工具跨账号访问拒绝：请求账号={account}，"
                f"会话已绑定={bound_account is not None}"
            )
            if bound_account is None:
                return None, error_result(
                    "权限不足",
                    "当前会话未绑定账号，非管理员不能通过 account 参数访问其他账号；"
                    "请先让管理员执行 /agently bind <账号名> 绑定。",
                )
            return None, error_result(
                "权限不足",
                "非管理员只能访问当前会话绑定的账号，不能通过 account 参数切换到其他账号。",
            )
    else:
        account, failure = await _resolve_account(plugin, event)
        if failure is not None:
            return None, failure
        if not isinstance(account, str) or not account.strip():
            logger.warning("[agently] 工具邮箱解析失败：绑定账号无效")
            return None, error_result("当前会话绑定的账号名无效")
        account = account.strip()
        if account in {".", ".."} or any(
            char in account for char in ("\x00", "/", "\\")
        ):
            logger.warning("[agently] 工具邮箱解析失败：账号含非法路径字符")
            return None, error_result("account 为空或含非法路径字符")

    requested_alias_id: str | None = None
    if "alias_id" in kwargs and kwargs["alias_id"] is not None:
        raw_alias_id = kwargs["alias_id"]
        if not isinstance(raw_alias_id, str) or not raw_alias_id.strip():
            logger.warning(f"[agently] 工具地址解析失败：账号={account}，地址标识不是非空字符串")
            return None, error_result("alias_id 必须是非空字符串")
        requested_alias_id = raw_alias_id.strip()
        if not _safe_path_identifier(requested_alias_id):
            logger.warning(f"[agently] 工具地址解析失败：账号={account}，地址标识含非法路径字符")
            return None, error_result("alias_id 含非法路径字符")

    try:
        if await plugin._token_store.load(account) is None:
            logger.warning(f"[agently] 工具凭证解析失败：账号={account}，原因=凭证不存在")
            return None, error_result(
                f"账号 {account!r} 没有可用凭证",
                "请先执行 /agently login <账号名>，或绑定已有账号。",
            )
    except TokenStoreError as exc:
        logger.warning(f"[agently] 工具凭证解析失败：账号={account}，异常={type(exc).__name__}")
        return None, error_result(f"读取账号 {account!r} 凭证失败：{exc}")

    client, _ = plugin._clients_for(account)
    try:
        me = await client.me()
    except TokenStoreError as exc:
        logger.warning(f"[agently] 工具凭证解析失败：账号={account}，异常={type(exc).__name__}")
        return None, error_result(f"读取账号 {account!r} 凭证失败：{exc}")

    selected_alias = None
    if requested_alias_id is None:
        selected_alias = next((alias for alias in me.aliases if alias.is_primary), None)
        if selected_alias is None:
            logger.warning(f"[agently] 工具地址解析失败：账号={account}，原因=缺少主地址")
            return None, error_result("/v1/me 未返回 is_primary 别名")
    else:
        selected_alias = next(
            (alias for alias in me.aliases if alias.alias_id == requested_alias_id),
            None,
        )
        if selected_alias is None:
            logger.warning(
                f"[agently] 工具地址解析失败：账号={account}，地址ID={requested_alias_id}，"
                "原因=地址不存在或不属于账号"
            )
            return None, error_result("邮箱地址不存在或不属于该账号")

    logger.debug(
        f"[agently] 工具邮箱解析完成：账号={account}，地址ID={selected_alias.alias_id}，"
        f"地址数量={len(me.aliases)}"
    )
    return (
        _ResolvedMailbox(
            account=account,
            client=client,
            me=me,
            alias_id=selected_alias.alias_id,
            email=selected_alias.email or selected_alias.alias_id,
        ),
        None,
    )


def _required_text(kwargs: dict[str, Any], key: str) -> str | None:
    """取必填字符串参数，缺失或空白时返回 None。"""
    value = kwargs.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _string_list(value: Any) -> list[str]:
    """把工具参数规整为去空白的字符串列表，非列表输入返回空列表。"""
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _whitelist_blocked(
    plugin: QQAgentlyMailPlugin,
    recipients: list[str],
) -> str | None:
    """返回白名单外的收件人串，未配置白名单或全部合规时返回 None。"""
    whitelist = plugin._config.get("mail_send_whitelist", []) or []
    if not whitelist:
        return None
    allowed = {str(item).strip().lower() for item in whitelist}
    blocked = [item for item in recipients if item.lower() not in allowed]
    return "、".join(blocked) if blocked else None


def _safe_filename(name: str) -> str:
    """取文件名的安全形态，去掉目录部分并拒绝路径特殊值。"""
    filename = Path(name).name
    if filename in {"", ".", ".."}:
        raise ValueError("附件文件名不合法")
    return filename


def _save_attachment(message_id: str, filename: str, content: bytes) -> Path:
    """把附件字节写入 AstrBot 临时目录并返回保存路径。"""
    directory = (
        Path(get_astrbot_temp_path())
        / "astrbot_plugin_qq_agentlymail"
        / "attachments"
        / message_id
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_bytes(content)
    return path


def _load_attachments(paths: list[str]) -> list[dict[str, Any]]:
    """读取本地附件文件并组装协议上传字段，超限直接抛错。"""
    if len(paths) > MAX_ATTACHMENT_COUNT:
        raise ValueError(f"附件数量超过上限 {MAX_ATTACHMENT_COUNT} 个")
    attachments: list[dict[str, Any]] = []
    total = 0
    for path_text in paths:
        path = Path(path_text)
        content = path.read_bytes()
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"附件 {path.name} 超过单附件 20MB 上限")
        total += len(content)
        if total > MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError("附件总量超过 20MB 上限")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        attachments.append(
            {
                "filename": path.name,
                "content_type": content_type,
                "size": len(content),
                "sha1": hashlib.sha1(content).hexdigest(),
                "content": base64.b64encode(content).decode("ascii"),
            }
        )
    return attachments


Handler = Callable[[AstrMessageEvent, dict[str, Any]], Awaitable[ToolExecResult]]


def _safe_tool_context(kwargs: dict[str, Any]) -> str:
    """只提取允许写入日志的标识、布尔开关和列表数量。"""
    fields: list[str] = []
    identifiers = (
        ("account", "账号"),
        ("alias_id", "地址ID"),
        ("message_id", "邮件ID"),
        ("dir", "目录"),
        ("action", "操作"),
    )
    for key, label in identifiers:
        value = kwargs.get(key)
        if isinstance(value, str) and value:
            fields.append(f"{label}={value}")
    switches = (
        ("unread", "只看未读"),
        ("reply_all", "回复全部"),
        ("include_attachments", "包含附件"),
    )
    for key, label in switches:
        value = kwargs.get(key)
        if isinstance(value, bool):
            fields.append(f"{label}={value}")
    counts = (
        ("to", "收件人数量"),
        ("cc", "抄送数量"),
        ("bcc", "密送数量"),
        ("attachment_paths", "附件数量"),
    )
    for key, label in counts:
        value = kwargs.get(key)
        if isinstance(value, list):
            fields.append(f"{label}={len(value)}")
    return "，".join(fields)


async def _dispatch_actions(
    tool: FunctionTool,
    actions: dict[str, Handler],
    event: AstrMessageEvent,
    **kwargs,
) -> ToolExecResult:
    """按 action 分发处理器，协议错误与未预期异常都转成 JSON 错误文本。"""
    started = time.perf_counter()
    context = _safe_tool_context(kwargs)
    action = kwargs.get("action")
    action = action if isinstance(action, str) else ""
    logger.debug(f"[agently] 工具调用开始：工具={tool.name}，{context}")
    handler = actions.get(action)
    if handler is None:
        logger.warning(f"[agently] 工具调用失败：工具={tool.name}，原因=未知操作，{context}")
        return error_result(
            f"未知 action：{action or '（空）'}",
            f"可选值：{'、'.join(actions)}。",
        )
    try:
        result = await handler(event, kwargs)
    except (ClientError, OAuthError) as exc:
        logger.error(
            f"[agently] 工具协议调用失败：工具={tool.name}，{context}，"
            f"异常={type(exc).__name__}，代码={getattr(exc, 'code', None)}，"
            f"状态码={getattr(exc, 'status_code', None)}，"
            f"耗时={time.perf_counter() - started:.3f}秒"
        )
        return error_result(str(exc))
    except Exception as exc:
        logger.exception(
            f"[agently] 工具调用异常：工具={tool.name}，{context}，"
            f"耗时={time.perf_counter() - started:.3f}秒"
        )
        return error_result(f"发生未预期的错误：{exc}")

    try:
        outcome = json.loads(result)
    except (TypeError, ValueError):
        logger.warning(
            f"[agently] 工具结果无法解析：工具={tool.name}，{context}，"
            f"耗时={time.perf_counter() - started:.3f}秒"
        )
        return result
    succeeded = isinstance(outcome, dict) and outcome.get("ok") is True
    message = "工具调用成功" if succeeded else "工具业务失败"
    log = logger.debug if succeeded else logger.warning
    log(
        f"[agently] {message}：工具={tool.name}，{context}，"
        f"耗时={time.perf_counter() - started:.3f}秒"
    )
    return result

@dataclass
class AgentlyMailReadTool(FunctionTool):
    """LLM 工具：读取绑定邮箱的身份、列表、全文、搜索与额度。"""

    def __init__(self, plugin: QQAgentlyMailPlugin):
        super().__init__(
            name="agently_mail_read",
            description=(
                "读取 QQ Agently Mail 邮箱；默认使用当前会话绑定账号的主地址，"
                "可用 alias_id 指定所属地址；非管理员仅可访问当前会话绑定的账号，"
                "仅管理员可用 account 跨会话指定其他已登录账号。"
                "action=whoami 查看选中账号与邮箱；list 按目录分页列摘要；"
                "read 按 message_id 读全文（可选下载附件）；search 按关键词搜索；"
                "quota 估算发信额度。"
            ),
            parameters=object_parameters(
                {
                    "action": enum_param(
                        "要执行的操作",
                        ["whoami", "list", "read", "search", "quota"],
                    ),
                    "account": string_param(
                        "可选账号名，缺省使用当前会话绑定账号；非管理员仅可指定当前会话已绑定的账号，管理员可指定任意已登录账号"
                    ),
                    "alias_id": string_param("可选邮箱地址 ID，缺省使用所选账号的主地址"),
                    "dir": enum_param(
                        "list 的邮件目录",
                        ["inbox", "sent", "trash", "spam"],
                        default="inbox",
                    ),
                    "unread": boolean_param("list 只看未读邮件"),
                    "limit": integer_param("list/search 每页条数，1-50，默认 10", default=10),
                    "cursor": string_param("list/search 翻页游标，来自上一页的 next_cursor"),
                    "message_id": string_param("read 的邮件 ID"),
                    "max_body_chars": integer_param(
                        f"read 正文最多输出的字符数，默认 {DEFAULT_MAX_BODY_CHARS}",
                        default=DEFAULT_MAX_BODY_CHARS,
                    ),
                    "attachment_id": string_param("read 可选，下载该附件并返回保存路径"),
                    "q": string_param("search 的搜索关键词"),
                    "search_in": enum_param(
                        "search 的搜索范围",
                        ["SEARCH_IN_ALL", "SEARCH_IN_SUBJECT", "SEARCH_IN_CONTENT"],
                        default="SEARCH_IN_ALL",
                    ),
                },
                required=["action"],
            ),
        )
        self._plugin = plugin
        self._actions: dict[str, Handler] = {
            "whoami": self._whoami,
            "list": self._list,
            "read": self._read,
            "search": self._search,
            "quota": self._quota,
        }

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        event = getattr(context.context, "event", None)
        if event is None:
            logger.warning("[agently] 工具调用拒绝：工具=agently_mail_read，原因=没有聊天会话")
            return error_result(
                "当前调用没有关联聊天会话",
                "请在聊天会话中使用该工具。",
            )
        return await _dispatch_actions(self, self._actions, event, **kwargs)


    async def _whoami(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        mailbox, failure = await _resolve_mailbox(self._plugin, event, kwargs)
        if failure is not None:
            return failure
        me = mailbox.me
        return ok_result(
            {
                "account": mailbox.account,
                "alias_id": mailbox.alias_id,
                "email": mailbox.email,
                "aliases": [
                    {
                        "alias_id": alias.alias_id,
                        "email": alias.email,
                        "is_primary": alias.is_primary,
                    }
                    for alias in me.aliases
                ],
                "rate_limits": me.rate_limits.values,
                "constraints": me.constraints.values,
                "scopes": me.scopes,
            }
        )

    async def _list(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        mailbox, failure = await _resolve_mailbox(self._plugin, event, kwargs)
        if failure is not None:
            return failure
        direction = kwargs.get("dir") or "inbox"
        limit = kwargs.get("limit", 10)
        cursor = kwargs.get("cursor")
        page = await mailbox.client.list_messages(
            direction=direction,
            limit=limit,
            cursor=cursor if isinstance(cursor, str) and cursor else None,
            unread=kwargs.get("unread"),
            alias_id=mailbox.alias_id,
        )
        return ok_result(
            {
                "account": mailbox.account,
                "alias_id": mailbox.alias_id,
                "dir": direction,
                "count": len(page.data),
                "has_more": page.has_more,
                "next_cursor": page.next_cursor,
                "messages": format_summary_list(page),
            }
        )

    async def _read(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        mailbox, failure = await _resolve_mailbox(self._plugin, event, kwargs)
        if failure is not None:
            return failure
        message_id = _required_text(kwargs, "message_id")
        if message_id is None:
            return error_result(
                "缺少 message_id 参数",
                "可先用 action=list 取邮件的 message_id。",
            )
        max_body_chars = kwargs.get("max_body_chars", DEFAULT_MAX_BODY_CHARS)
        if (
            isinstance(max_body_chars, bool)
            or not isinstance(max_body_chars, int)
            or max_body_chars <= 0
        ):
            return error_result("max_body_chars 必须是正整数")
        attachment_id = kwargs.get("attachment_id")
        if attachment_id is not None and not isinstance(attachment_id, str):
            return error_result("attachment_id 必须是字符串")
        if not _safe_path_identifier(message_id):
            return error_result("message_id 含非法路径字符")
        if attachment_id and not _safe_path_identifier(attachment_id):
            return error_result("attachment_id 含非法路径字符")
        message = await mailbox.client.read(message_id, alias_id=mailbox.alias_id)
        data: dict[str, Any] = {
            "account": mailbox.account,
            "alias_id": mailbox.alias_id,
            "message": format_message(message, max_body_chars),
        }
        if attachment_id:
            attachment = next(
                (
                    item
                    for item in message.attachments
                    if item.attachment_id == attachment_id
                ),
                None,
            )
            if attachment is None:
                return error_result(f"邮件 {message_id} 没有附件 {attachment_id}")
            content = await mailbox.client.download(
                message_id,
                attachment_id,
                alias_id=mailbox.alias_id,
            )
            try:
                filename = _safe_filename(attachment.filename or attachment_id)
                path = await asyncio.to_thread(
                    _save_attachment,
                    message_id,
                    filename,
                    content,
                )
            except (OSError, ValueError) as exc:
                logger.warning(
                    f"[agently] 工具附件写入失败：账号={mailbox.account}，"
                    f"地址ID={mailbox.alias_id}，邮件ID={message_id}，附件数量=1，"
                    f"异常={type(exc).__name__}"
                )
                raise
            data["saved_attachment"] = {
                "attachment_id": attachment_id,
                "path": str(path),
                "size": len(content),
            }
        return ok_result(data)

    async def _search(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        mailbox, failure = await _resolve_mailbox(self._plugin, event, kwargs)
        if failure is not None:
            return failure
        query = _required_text(kwargs, "q")
        if query is None:
            return error_result("缺少 q 参数", "search 需要搜索关键词。")
        search_in = kwargs.get("search_in") or "SEARCH_IN_ALL"
        limit = kwargs.get("limit", 10)
        cursor = kwargs.get("cursor")
        page = await mailbox.client.search(
            query,
            limit=limit,
            search_in=search_in,
            cursor=cursor if isinstance(cursor, str) and cursor else None,
            alias_id=mailbox.alias_id,
        )
        return ok_result(
            {
                "account": mailbox.account,
                "alias_id": mailbox.alias_id,
                "q": query,
                "search_in": search_in,
                "count": len(page.data),
                "has_more": page.has_more,
                "next_cursor": page.next_cursor,
                "messages": format_summary_list(page),
            }
        )

    async def _quota(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        mailbox, failure = await _resolve_mailbox(self._plugin, event, kwargs)
        if failure is not None:
            return failure
        sent_page = await mailbox.client.list_messages(
            direction="sent",
            limit=50,
            alias_id=mailbox.alias_id,
        )
        sent_count = len(sent_page.data)
        return ok_result(
            {
                "account": mailbox.account,
                "alias_id": mailbox.alias_id,
                "rate_limits": mailbox.me.rate_limits.values,
                "sent_today_local": await self._plugin._send_count_today(mailbox.account),
                "sent_folder_first_page": "50+" if sent_count >= 50 else sent_count,
            }
        )


@dataclass
class AgentlyMailSendTool(FunctionTool):
    """LLM 工具：用绑定邮箱发信、回复、转发并监听新邮件。"""

    def __init__(self, plugin: QQAgentlyMailPlugin):
        super().__init__(
            name="agently_mail_send",
            description=(
                "用 QQ Agently Mail 邮箱发信；send/reply/forward/watch_start 默认使用当前会话绑定账号的主地址，"
                "可用 alias_id 指定所属地址；非管理员仅可访问当前会话绑定的账号，"
                "仅管理员可用 account 跨会话指定其他已登录账号；"
                "action=send 发新邮件（可带本地附件）；reply 回复指定邮件；"
                "forward 转发指定邮件；watch_start 监听所选地址，"
                "watch_stop 停止当前会话唯一监听；收到新邮件会自动推送到本会话。"
            ),
            parameters=object_parameters(
                {
                    "action": enum_param(
                        "要执行的操作",
                        ["send", "reply", "forward", "watch_start", "watch_stop"],
                    ),
                    "account": string_param(
                        "send/reply/forward/watch_start 可选账号名，缺省使用当前会话绑定账号；非管理员仅可指定当前会话已绑定的账号，管理员可指定任意已登录账号"
                    ),
                    "alias_id": string_param("send/reply/forward/watch_start 可选邮箱地址 ID，缺省使用所选账号的主地址"),
                    "to": string_array_param("send/forward 的收件人邮箱地址"),
                    "cc": string_array_param("send 的抄送邮箱地址"),
                    "bcc": string_array_param("send 的密送邮箱地址"),
                    "subject": string_param("send 的邮件主题"),
                    "body": string_param("send/reply/forward 的邮件正文"),
                    "body_format": enum_param(
                        "正文格式",
                        ["HTML", "PLAIN", "MARKDOWN"],
                        default="HTML",
                    ),
                    "attachment_paths": string_array_param("send 的本地附件文件路径"),
                    "message_id": string_param("reply/forward 的原邮件 ID"),
                    "reply_all": boolean_param(
                        "reply 是否回复全部收件人，默认只回发件人",
                        default=False,
                    ),
                    "include_attachments": boolean_param(
                        "forward 是否附带原邮件附件，默认附带",
                        default=True,
                    ),
                },
                required=["action"],
            ),
        )
        self._plugin = plugin
        self._actions: dict[str, Handler] = {
            "send": self._send,
            "reply": self._reply,
            "forward": self._forward,
            "watch_start": self._watch_start,
            "watch_stop": self._watch_stop,
        }

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        event = getattr(context.context, "event", None)
        if event is None:
            logger.warning("[agently] 工具调用拒绝：工具=agently_mail_send，原因=没有聊天会话")
            return error_result(
                "当前调用没有关联聊天会话",
                "请在聊天会话中使用该工具。",
            )
        return await _dispatch_actions(self, self._actions, event, **kwargs)

    def _body_of(self, kwargs: dict[str, Any]) -> str | None:
        body = kwargs.get("body")
        if not isinstance(body, str) or not body:
            return None
        return body

    def _check_whitelist(self, recipients: list[str]) -> ToolExecResult | None:
        blocked = _whitelist_blocked(self._plugin, recipients)
        if blocked is not None:
            return error_result(
                "收件人不在发件白名单内",
                f"未被允许的地址：{blocked}；可在插件配置 mail_send_whitelist 中调整。",
            )
        return None

    @staticmethod
    def _reply_recipients(message: Any, reply_all: bool) -> list[str]:
        """按原邮件推导 reply/reply_all 的收件人，供白名单校验。"""
        senders = []
        if message.from_ is not None:
            senders.append(message.from_)
        if reply_all:
            senders.extend(message.to)
            senders.extend(message.cc)
        return [sender.email for sender in senders if sender.email]


    async def _send(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        mailbox, failure = await _resolve_mailbox(self._plugin, event, kwargs)
        if failure is not None:
            return failure
        to = _string_list(kwargs.get("to"))
        if not to:
            return error_result("缺少 to 参数", "send 需要至少一个收件人邮箱。")
        cc = _string_list(kwargs.get("cc"))
        bcc = _string_list(kwargs.get("bcc"))
        subject = _required_text(kwargs, "subject")
        if subject is None:
            return error_result("缺少 subject 参数")
        body = self._body_of(kwargs)
        if body is None:
            return error_result("缺少 body 参数")
        body_format = kwargs.get("body_format") or "HTML"
        attachment_paths = _string_list(kwargs.get("attachment_paths"))
        blocked = self._check_whitelist(to + cc + bcc)
        if blocked is not None:
            return blocked
        try:
            attachments = await asyncio.to_thread(_load_attachments, attachment_paths)
        except OSError as exc:
            logger.warning(
                f"[agently] 工具附件读取失败：账号={mailbox.account}，"
                f"地址ID={mailbox.alias_id}，附件数量={len(attachment_paths)}，"
                f"异常={type(exc).__name__}"
            )
            return error_result(f"读取附件文件失败：{exc}")
        except ValueError as exc:
            logger.warning(
                f"[agently] 工具附件校验失败：账号={mailbox.account}，"
                f"地址ID={mailbox.alias_id}，附件数量={len(attachment_paths)}"
            )
            return error_result(str(exc))
        await mailbox.client.send(
            to=to,
            subject=subject,
            body=body,
            body_format=body_format,
            alias_id=mailbox.alias_id,
            cc=cc,
            bcc=bcc,
            attachments=attachments,
        )
        await self._plugin._record_send(mailbox.account)
        return ok_result(
            {
                "account": mailbox.account,
                "alias_id": mailbox.alias_id,
                "queued": True,
                "to": to,
                "subject": subject,
                "attachments": len(attachments),
            }
        )

    async def _reply(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        mailbox, failure = await _resolve_mailbox(self._plugin, event, kwargs)
        if failure is not None:
            return failure
        message_id = _required_text(kwargs, "message_id")
        if message_id is None:
            return error_result("缺少 message_id 参数")
        body = self._body_of(kwargs)
        if body is None:
            return error_result("缺少 body 参数")
        body_format = kwargs.get("body_format") or "HTML"
        reply_all = kwargs.get("reply_all", False)
        if not isinstance(reply_all, bool):
            return error_result("reply_all 必须是布尔值")
        if self._plugin._config.get("mail_send_whitelist"):
            original = await mailbox.client.read(
                message_id,
                alias_id=mailbox.alias_id,
            )
            recipients = self._reply_recipients(original, reply_all)
            if not recipients:
                return error_result(
                    "无法确定回复收件人",
                    "已配置 mail_send_whitelist，无法安全校验本次回复的收件人。",
                )
            blocked = self._check_whitelist(recipients)
            if blocked is not None:
                return blocked
        await mailbox.client.reply(
            message_id,
            body=body,
            body_format=body_format,
            reply_all=reply_all,
            alias_id=mailbox.alias_id,
        )
        await self._plugin._record_send(mailbox.account)
        return ok_result(
            {
                "account": mailbox.account,
                "alias_id": mailbox.alias_id,
                "queued": True,
                "message_id": message_id,
                "reply_all": reply_all,
            }
        )

    async def _forward(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        mailbox, failure = await _resolve_mailbox(self._plugin, event, kwargs)
        if failure is not None:
            return failure
        message_id = _required_text(kwargs, "message_id")
        if message_id is None:
            return error_result("缺少 message_id 参数")
        to = _string_list(kwargs.get("to"))
        if not to:
            return error_result("缺少 to 参数", "forward 需要至少一个收件人邮箱。")
        body = self._body_of(kwargs)
        if body is None:
            return error_result("缺少 body 参数")
        body_format = kwargs.get("body_format") or "HTML"
        include_attachments = kwargs.get("include_attachments", True)
        if not isinstance(include_attachments, bool):
            return error_result("include_attachments 必须是布尔值")
        blocked = self._check_whitelist(to)
        if blocked is not None:
            return blocked
        await mailbox.client.forward(
            message_id,
            to=to,
            body=body,
            body_format=body_format,
            include_attachments=include_attachments,
            alias_id=mailbox.alias_id,
        )
        await self._plugin._record_send(mailbox.account)
        return ok_result(
            {
                "account": mailbox.account,
                "alias_id": mailbox.alias_id,
                "queued": True,
                "message_id": message_id,
                "to": to,
                "include_attachments": include_attachments,
            }
        )

    async def _watch_start(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        mailbox, failure = await _resolve_mailbox(self._plugin, event, kwargs)
        if failure is not None:
            return failure
        error = await self._plugin._start_watch(
            event.unified_msg_origin,
            mailbox.account,
            mailbox.alias_id,
        )
        if error is not None:
            return error_result(error)
        return ok_result(
            {
                "account": mailbox.account,
                "alias_id": mailbox.alias_id,
                "email": mailbox.email,
                "message": (
                    f"已开始监听账号 {mailbox.account} 的地址 {mailbox.email}，"
                    "收到新邮件时会自动推送到本会话。"
                ),
            }
        )

    async def _watch_stop(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        found, error = await self._plugin._stop_watch(event.unified_msg_origin)
        if error is not None:
            return error_result(error)
        if not found:
            return error_result("当前会话没有在监听新邮件。")
        return ok_result({"message": "已停止新邮件监听。"})

@dataclass
class AgentlyMailAdminTool(FunctionTool):
    """LLM 工具：管理员管理账号登录、邮件删除与邮箱地址。"""

    def __init__(self, plugin: QQAgentlyMailPlugin):
        super().__init__(
            name="agently_mail_admin",
            description=(
                "管理员专用：管理 QQ Agently Mail 账号、邮件删除与邮箱地址。"
                "action=login 发起微信扫码登录，二维码、链接和验证码会发到本会话；多邮箱账号会要求回复编号；"
                "logout 删除账号凭证并解绑；status 列出账号与当前会话绑定；"
                "trash 把邮件移入垃圾箱；empty_trash 清空垃圾箱；"
                "permanent_delete 永久删除邮件；"
                "addresses 列出全部邮箱地址、昵称与别名配额；"
                "create_address 新建邮箱地址，前缀由服务端派生，不可指定；"
                "delete_address 注销邮箱地址：不可恢复、邮件全删、已绑定 Agent 自动解绑，"
                "90 天保留期内新建地址会复用旧地址，执行前必须向用户确认。"
            ),
            parameters=object_parameters(
                {
                    "action": enum_param(
                        "要执行的操作",
                        [
                            "login",
                            "logout",
                            "status",
                            "trash",
                            "empty_trash",
                            "permanent_delete",
                            "addresses",
                            "create_address",
                            "delete_address",
                        ],
                    ),
                    "account": string_param("login/logout 的账号名，默认 default", default="default"),
                    "message_id": string_param("trash/permanent_delete 的邮件 ID"),
                    "alias_id": string_param("delete_address 要注销的地址 ID，可先用 addresses 查看"),
                },
                required=["action"],
            ),
        )
        self._plugin = plugin
        self._actions: dict[str, Handler] = {
            "login": self._login,
            "logout": self._logout,
            "status": self._status,
            "trash": self._trash,
            "empty_trash": self._empty_trash,
            "permanent_delete": self._permanent_delete,
            "addresses": self._addresses,
            "create_address": self._create_address,
            "delete_address": self._delete_address,
        }

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        event = getattr(context.context, "event", None)
        if event is None or not event.is_admin():
            logger.warning("[agently] 工具调用拒绝：工具=agently_mail_admin，原因=权限不足或没有聊天会话")
            return error_result("权限不足", "QQ Agently Mail 管理工具仅管理员可用。")
        return await _dispatch_actions(self, self._actions, event, **kwargs)

    def _account_of(self, kwargs: dict[str, Any]) -> str:
        """取可选账号名参数，缺省 default，非法输入返回空串。"""
        account = kwargs.get("account") or "default"
        if not isinstance(account, str) or not account.strip():
            return ""
        return account.strip()

    async def _login(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        account = self._account_of(kwargs)
        if not account:
            return error_result("account 必须是非空字符串")
        error = await self._plugin._start_login(event, account)
        if error is not None:
            return error_result(error)
        return ok_result(
            {
                "account": account,
                "message": (
                    f"账号 {account} 登录已发起，二维码、链接和验证码已发送到本会话，"
                    "请扫码并在手机上确认；如果出现多个邮箱，请回复编号；结果会自动推送。"
                ),
            }
        )

    async def _logout(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        account = self._account_of(kwargs)
        if not account:
            return error_result("account 必须是非空字符串")
        deleted, unbound, error = await self._plugin._remove_account(account)
        if error is not None:
            return error_result(error)
        if not deleted:
            return error_result(f"账号 {account} 没有已保存的凭证。")
        return ok_result(
            {
                "account": account,
                "unbound_sessions": unbound,
                "message": f"已删除账号 {account} 的凭证，并解除 {unbound} 个会话的绑定。",
            }
        )

    async def _status(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        snapshot = await self._plugin._account_snapshot(event.unified_msg_origin)
        return ok_result(snapshot)

    async def _trash(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        account, failure = await _resolve_account(self._plugin, event)
        if failure is not None:
            return failure
        message_id = _required_text(kwargs, "message_id")
        if message_id is None:
            return error_result("缺少 message_id 参数")
        client, _ = self._plugin._clients_for(account)
        await client.trash(message_id)
        return ok_result(
            {
                "account": account,
                "message_id": message_id,
                "message": "已把邮件移入垃圾箱。",
            }
        )

    async def _empty_trash(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        account, failure = await _resolve_account(self._plugin, event)
        if failure is not None:
            return failure
        client, _ = self._plugin._clients_for(account)
        await client.empty_trash()
        return ok_result({"account": account, "message": "已清空垃圾箱。"})

    async def _permanent_delete(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        account, failure = await _resolve_account(self._plugin, event)
        if failure is not None:
            return failure
        message_id = _required_text(kwargs, "message_id")
        if message_id is None:
            return error_result("缺少 message_id 参数")
        client, _ = self._plugin._clients_for(account)
        await client.permanent_delete(message_id)
        return ok_result(
            {
                "account": account,
                "message_id": message_id,
                "message": "已永久删除邮件，不可恢复。",
            }
        )

    async def _addresses(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        account, failure = await _resolve_account(self._plugin, event)
        if failure is not None:
            return failure
        client, _ = self._plugin._clients_for(account)
        me = await client.me()
        account_info = await client.get_account()
        return ok_result(
            {
                "account": account,
                "nickname": account_info.get("nickname"),
                "agent_alias_limit": account_info.get("agent_alias_limit"),
                "aliases": [
                    {
                        "alias_id": alias.alias_id,
                        "email": alias.email,
                        "is_primary": alias.is_primary,
                    }
                    for alias in me.aliases
                ],
            }
        )

    async def _create_address(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        account, failure = await _resolve_account(self._plugin, event)
        if failure is not None:
            return failure
        client, _ = self._plugin._clients_for(account)
        created = await client.create_address()
        return ok_result(
            {
                "account": account,
                **created,
                "message": (
                    "已创建并绑定新邮箱地址。前缀由服务端派生，无法指定；"
                    "is_reused 为 true 表示复用了 90 天注销保留期内的旧地址。"
                ),
            }
        )

    async def _delete_address(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        account, failure = await _resolve_account(self._plugin, event)
        if failure is not None:
            return failure
        alias_id = _required_text(kwargs, "alias_id")
        if alias_id is None:
            return error_result(
                "缺少 alias_id 参数",
                "可先用 action=addresses 查看要注销地址的 alias_id。",
            )
        client, _ = self._plugin._clients_for(account)
        await client.delete_address(alias_id)
        return ok_result(
            {
                "account": account,
                "alias_id": alias_id,
                "message": (
                    "已注销邮箱地址：邮件全部删除，已绑定的 Agent 自动解绑，不可恢复；"
                    "90 天保留期内新建地址将复用该地址。"
                ),
            }
        )


def make_tools(plugin: QQAgentlyMailPlugin) -> list[FunctionTool]:
    """构造插件暴露给 LLM 的三个工具实例。"""
    return [
        AgentlyMailReadTool(plugin),
        AgentlyMailSendTool(plugin),
        AgentlyMailAdminTool(plugin),
    ]
