"""定义插件暴露给 LLM 的三个 FunctionTool 与守卫、结果、schema 助手。"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import json
import mimetypes

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .client import ClientError
from .oauth import OAuthError
from .render import format_message, format_summary_list
from .token_store import DEFAULT_STORAGE_ROOT

if TYPE_CHECKING:
    from ..main import QQAgentlyMailPlugin

# 附件上限取自 artifacts/api-map.md 的运行时约束观测值，服务器仍是最终权威。
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENT_COUNT = 50
MAX_TOTAL_ATTACHMENT_BYTES = 20 * 1024 * 1024

# read 下载附件时的缺省正文截断与目录名安全字符。
DEFAULT_MAX_BODY_CHARS = 8000
ATTACHMENTS_ROOT = DEFAULT_STORAGE_ROOT / "attachments"

def _safe_path_identifier(value: str) -> bool:
    """判断协议标识是否可以安全地出现在 URL 和附件目录名中。"""
    return bool(value) and all(
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
        return None, error_result(
            "当前会话未绑定账号",
            "请先执行 /agently bind <账号名> 绑定已有账号，或让管理员执行 /agently login。",
        )
    return account, None


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
    """把附件字节写入插件数据目录并返回保存路径。"""
    directory = ATTACHMENTS_ROOT / message_id
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


async def _dispatch_actions(
    tool: FunctionTool,
    actions: dict[str, Handler],
    event: AstrMessageEvent,
    **kwargs,
) -> ToolExecResult:
    """按 action 分发处理器，协议错误与未预期异常都转成 JSON 错误文本。"""
    action = kwargs.get("action")
    action = action if isinstance(action, str) else ""
    handler = actions.get(action)
    if handler is None:
        return error_result(
            f"未知 action：{action or '（空）'}",
            f"可选值：{'、'.join(actions)}。",
        )
    try:
        return await handler(event, kwargs)
    except (ClientError, OAuthError) as exc:
        logger.error(f"[agently] 工具 {tool.name}（{action}）失败: {exc}")
        return error_result(str(exc))
    except Exception as exc:
        logger.exception(f"[agently] 工具 {tool.name}（{action}）异常")
        return error_result(f"发生未预期的错误：{exc}")


@dataclass
class AgentlyMailReadTool(FunctionTool):
    """LLM 工具：读取绑定邮箱的身份、列表、全文、搜索与额度。"""

    def __init__(self, plugin: QQAgentlyMailPlugin):
        super().__init__(
            name="agently_mail_read",
            description=(
                "读取当前会话绑定的 QQ Agently Mail 邮箱。"
                "action=whoami 查看绑定账号与邮箱；list 按目录分页列摘要；"
                "read 按 message_id 读全文（可选下载附件）；search 按关键词搜索；"
                "quota 估算发信额度。"
            ),
            parameters=object_parameters(
                {
                    "action": enum_param(
                        "要执行的操作",
                        ["whoami", "list", "read", "search", "quota"],
                    ),
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
        account, failure = await _resolve_account(self._plugin, event)
        if failure is not None:
            return failure
        client, _ = self._plugin._clients_for(account)
        me = await client.me()
        return ok_result(
            {
                "account": account,
                "email": self._plugin._primary_email(me),
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
        account, failure = await _resolve_account(self._plugin, event)
        if failure is not None:
            return failure
        direction = kwargs.get("dir") or "inbox"
        limit = kwargs.get("limit", 10)
        cursor = kwargs.get("cursor")
        client, _ = self._plugin._clients_for(account)
        page = await client.list_messages(
            direction=direction,
            limit=limit,
            cursor=cursor if isinstance(cursor, str) and cursor else None,
            unread=kwargs.get("unread"),
        )
        return ok_result(
            {
                "account": account,
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
        account, failure = await _resolve_account(self._plugin, event)
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
        client, _ = self._plugin._clients_for(account)
        message = await client.read(message_id)
        data: dict[str, Any] = {"message": format_message(message, max_body_chars)}
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
            content = await client.download(message_id, attachment_id)
            filename = _safe_filename(attachment.filename or attachment_id)
            path = await asyncio.to_thread(
                _save_attachment,
                message_id,
                filename,
                content,
            )
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
        account, failure = await _resolve_account(self._plugin, event)
        if failure is not None:
            return failure
        query = _required_text(kwargs, "q")
        if query is None:
            return error_result("缺少 q 参数", "search 需要搜索关键词。")
        search_in = kwargs.get("search_in") or "SEARCH_IN_ALL"
        limit = kwargs.get("limit", 10)
        cursor = kwargs.get("cursor")
        client, _ = self._plugin._clients_for(account)
        page = await client.search(
            query,
            limit=limit,
            search_in=search_in,
            cursor=cursor if isinstance(cursor, str) and cursor else None,
        )
        return ok_result(
            {
                "account": account,
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
        account, failure = await _resolve_account(self._plugin, event)
        if failure is not None:
            return failure
        client, _ = self._plugin._clients_for(account)
        me = await client.me()
        sent_page = await client.list_messages(direction="sent", limit=50)
        sent_count = len(sent_page.data)
        return ok_result(
            {
                "account": account,
                "rate_limits": me.rate_limits.values,
                "sent_today_local": await self._plugin._send_count_today(account),
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
                "用当前会话绑定的 QQ Agently Mail 邮箱发信。"
                "action=send 发新邮件（可带本地附件）；reply 回复指定邮件；"
                "forward 转发指定邮件；watch_start/watch_stop 启停新邮件监听，"
                "收到新邮件会自动推送到本会话。"
            ),
            parameters=object_parameters(
                {
                    "action": enum_param(
                        "要执行的操作",
                        ["send", "reply", "forward", "watch_start", "watch_stop"],
                    ),
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
        account, failure = await _resolve_account(self._plugin, event)
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
            return error_result(f"读取附件文件失败：{exc}")
        except ValueError as exc:
            return error_result(str(exc))
        client, _ = self._plugin._clients_for(account)
        await client.send(
            to=to,
            subject=subject,
            body=body,
            body_format=body_format,
            cc=cc,
            bcc=bcc,
            attachments=attachments,
        )
        await self._plugin._record_send(account)
        return ok_result(
            {
                "account": account,
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
        account, failure = await _resolve_account(self._plugin, event)
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
        client, _ = self._plugin._clients_for(account)
        if self._plugin._config.get("mail_send_whitelist"):
            original = await client.read(message_id)
            recipients = self._reply_recipients(original, reply_all)
            if not recipients:
                return error_result(
                    "无法确定回复收件人",
                    "已配置 mail_send_whitelist，无法安全校验本次回复的收件人。",
                )
            blocked = self._check_whitelist(recipients)
            if blocked is not None:
                return blocked
        await client.reply(
            message_id,
            body=body,
            body_format=body_format,
            reply_all=reply_all,
        )
        await self._plugin._record_send(account)
        return ok_result(
            {
                "account": account,
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
        account, failure = await _resolve_account(self._plugin, event)
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
        client, _ = self._plugin._clients_for(account)
        await client.forward(
            message_id,
            to=to,
            body=body,
            body_format=body_format,
            include_attachments=include_attachments,
        )
        await self._plugin._record_send(account)
        return ok_result(
            {
                "account": account,
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
        account, failure = await _resolve_account(self._plugin, event)
        if failure is not None:
            return failure
        error = await self._plugin._start_watch(event.unified_msg_origin, account)
        if error is not None:
            return error_result(error)
        return ok_result(
            {
                "account": account,
                "message": f"已开始监听账号 {account} 的新邮件，收到时会自动推送到本会话。",
            }
        )

    async def _watch_stop(
        self,
        event: AstrMessageEvent,
        kwargs: dict[str, Any],
    ) -> ToolExecResult:
        error = self._plugin._stop_watch(event.unified_msg_origin)
        if error is not None:
            return error_result(error)
        return ok_result({"message": "已停止新邮件监听。"})


@dataclass
class AgentlyMailAdminTool(FunctionTool):
    """LLM 工具：管理员管理账号登录与邮件删除。"""

    def __init__(self, plugin: QQAgentlyMailPlugin):
        super().__init__(
            name="agently_mail_admin",
            description=(
                "管理员专用：管理 QQ Agently Mail 账号与邮件删除。"
                "action=login 发起微信扫码登录，二维码、链接和验证码会发到本会话；多邮箱账号会要求回复编号；"
                "logout 删除账号凭证并解绑；status 列出账号与当前会话绑定；"
                "trash 把邮件移入垃圾箱；empty_trash 清空垃圾箱；"
                "permanent_delete 永久删除邮件。"
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
                        ],
                    ),
                    "account": string_param("login/logout 的账号名，默认 default", default="default"),
                    "message_id": string_param("trash/permanent_delete 的邮件 ID"),
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
        }

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        event = getattr(context.context, "event", None)
        if event is None or not event.is_admin():
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
        deleted, unbound = await self._plugin._remove_account(account)
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



def make_tools(plugin: QQAgentlyMailPlugin) -> list[FunctionTool]:
    """构造插件暴露给 LLM 的三个工具实例。"""
    return [
        AgentlyMailReadTool(plugin),
        AgentlyMailSendTool(plugin),
        AgentlyMailAdminTool(plugin),
    ]
