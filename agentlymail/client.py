"""封装 QQ Agently Mail 的认证请求和全部协议端点。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

from astrbot.api import logger
from .constants import API_BASE, USER_AGENT
from .models import (
    Attachment,
    Constraints,
    Me,
    Message,
    MessageSummary,
    Page,
    RateLimits,
    Sender,
)
from .oauth import OAuthClient
from .token_store import TokenStore

# 别名管理端点在 agent.qq.com 主站，直接认 OAuth Bearer，不走 API 子域。
MGR_URL = "https://agent.qq.com/agent/mgr"
GETACCOUNT_URL = "https://agent.qq.com/agent/getaccount"
ALIAS_DOMAIN = "@agent.qq.com"
_ALIAS_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{5,17}$")
_MGR_ERROR_MESSAGES: dict[str, str] = {
    "-75103": "该邮箱地址不可修改",
    "-75104": "该邮箱地址已被占用",
    "-75105": "今日检测次数已用完",
}


def normalize_alias_email(value: str) -> str:
    """规范化并校验邮箱地址前缀或完整地址，返回 `<prefix>@agent.qq.com`。"""
    if not isinstance(value, str):
        raise ClientError(
            "邮箱前缀必须是字符串",
            status_code=400,
            code="INVALID_ALIAS",
        )
    text = value.strip().lower()
    if not text:
        raise ClientError(
            "邮箱前缀不能为空",
            status_code=400,
            code="INVALID_ALIAS",
        )
    if "@" in text:
        prefix, _, domain = text.partition("@")
        if f"@{domain}" != ALIAS_DOMAIN:
            raise ClientError(
                f"邮箱地址后缀必须是 {ALIAS_DOMAIN}",
                status_code=400,
                code="INVALID_ALIAS",
            )
    else:
        prefix = text
    if not _ALIAS_PREFIX_RE.fullmatch(prefix):
        raise ClientError(
            "邮箱前缀须以字母开头，长度为 6-18 位小写字母或数字",
            status_code=400,
            code="INVALID_ALIAS",
        )
    return f"{prefix}{ALIAS_DOMAIN}"

# 附件下载单次内存上限（略高于协议 20MB 附件限制，防止异常响应耗尽内存）。
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024


def _validate_download_url(value: str) -> None:
    """校验外链附件下载地址仅允许公网 HTTPS，拒绝本地与私网目标。"""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ClientError("下载附件失败: 无效的 download_url") from exc
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise ClientError("下载附件失败: download_url 必须是标准 HTTPS 地址")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ClientError("下载附件失败: 拒绝访问本地或内网 download_url")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise ClientError("下载附件失败: 拒绝访问非公网 IP 的 download_url")



def _safe_endpoint(value: str) -> str:
    """返回不含 query 和用户信息的安全请求目标。"""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid-url>"
    return f"{parsed.hostname or '-'}{parsed.path or '/'}"


class ClientError(RuntimeError):
    """表示协议客户端请求或响应处理失败。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        details: Any = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details


Sleep = Callable[[float], Awaitable[None]]


class AgentlyMailClient:
    """通过 Bearer 凭证访问 QQ Agently Mail 协议端点。"""

    def __init__(
        self,
        token_store: TokenStore,
        account: str,
        http_client: httpx.AsyncClient | None = None,
        oauth_client: OAuthClient | None = None,
        sleep: Sleep | None = None,
    ):
        self.token_store = token_store
        self.account = account
        self._http = http_client or httpx.AsyncClient(base_url=API_BASE)
        self._owns_http = http_client is None
        self._oauth = oauth_client or OAuthClient(token_store, account)
        self._owns_oauth = oauth_client is None
        self._sleep = sleep or asyncio.sleep
        self._alias_id: str | None = None
        self._me: Me | None = None
        self.rate_limits: RateLimits | None = None
        self.constraints: Constraints | None = None

    async def close(self) -> None:
        """关闭客户端自行创建的 HTTP 资源。"""
        if self._owns_oauth:
            await self._oauth.close()
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> AgentlyMailClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    async def me(self) -> Me:
        """读取身份、别名、运行时限额和运行时约束。"""
        logger.debug(f"[agently] 客户端操作开始 操作=me 账号={self.account}")
        try:
            payload = await self._request_json("GET", "/v1/me", "读取身份")
            me = Me.from_payload(self._data_mapping(payload, "读取身份"))
            self._me = me
            self.rate_limits = me.rate_limits
            self.constraints = me.constraints
            if self._alias_id is None:
                primary = next((alias for alias in me.aliases if alias.is_primary), None)
                if primary is not None:
                    self._alias_id = primary.alias_id
            logger.debug(
                f"[agently] 客户端操作完成 操作=me 账号={self.account} "
                f"地址数量={len(me.aliases)}"
            )
            return me
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=me 账号={self.account} "
                f"异常={type(exc).__name__}"
            )
            raise

    async def list_messages(
        self,
        *,
        alias_id: str | None = None,
        limit: int = 50,
        direction: str = "inbox",
        cursor: str | None = None,
        unread: bool | None = None,
    ) -> Page[MessageSummary]:
        """分页读取指定别名的邮件列表，unread 为 True 时只取未读。"""
        logger.debug(
            f"[agently] 客户端操作开始 操作=list 账号={self.account} "
            f"地址ID={alias_id} 目录={direction} 每页数量={limit} 只看未读={unread} "
            f"游标存在={cursor is not None}"
        )
        try:
            alias = await self._resolve_alias(alias_id)
            self._validate_limit(limit)
            if unread is not None and not isinstance(unread, bool):
                raise ClientError("unread 必须是布尔值")
            if direction not in {"inbox", "sent", "trash", "spam"}:
                raise ClientError(f"不支持的邮件目录: {direction}")
            params: dict[str, Any] = {"limit": limit, "dir": direction}
            if cursor is not None:
                params["cursor"] = cursor
            if unread:
                params["unread"] = "true"
            payload = await self._request_json(
                "GET",
                f"/v1/aliases/{alias}/messages",
                "读取邮件列表",
                params=params,
            )
            page = Page.from_payload(payload, MessageSummary.from_payload)
            logger.debug(
                f"[agently] 客户端操作完成 操作=list 账号={self.account} "
                f"地址ID={alias} 数量={len(page.data)} 还有更多={page.has_more} "
                f"下一游标存在={bool(page.next_cursor)}"
            )
            return page
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=list 账号={self.account} "
                f"异常={type(exc).__name__}"
            )
            raise

    async def read(
        self,
        message_id: str,
        *,
        alias_id: str | None = None,
    ) -> Message:
        """读取指定邮件的完整内容。"""
        logger.debug(
            f"[agently] 客户端操作开始 操作=read 账号={self.account} "
            f"地址ID={alias_id} 邮件ID={message_id}"
        )
        try:
            alias = await self._resolve_alias(alias_id)
            self._require_identifier(message_id, "message_id")
            payload = await self._request_json(
                "GET",
                f"/v1/aliases/{alias}/messages/{message_id}",
                "读取邮件",
            )
            message = Message.from_payload(self._data_mapping(payload, "读取邮件"))
            logger.debug(
                f"[agently] 客户端操作完成 操作=read 账号={self.account} "
                f"地址ID={alias} 邮件ID={message_id} "
                f"附件数量={len(message.attachments)}"
            )
            return message
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=read 账号={self.account} "
                f"邮件ID={message_id} 异常={type(exc).__name__}"
            )
            raise

    async def search(
        self,
        query: str,
        *,
        alias_id: str | None = None,
        limit: int = 50,
        search_in: str = "SEARCH_IN_ALL",
        cursor: str | None = None,
    ) -> Page[MessageSummary]:
        """按原始 q 参数分页搜索邮件。"""
        logger.debug(
            f"[agently] 客户端操作开始 操作=search 账号={self.account} "
            f"地址ID={alias_id} 每页数量={limit} 搜索范围={search_in} "
            f"游标存在={cursor is not None}"
        )
        try:
            alias = await self._resolve_alias(alias_id)
            self._validate_limit(limit)
            if search_in not in {
                "SEARCH_IN_ALL",
                "SEARCH_IN_SUBJECT",
                "SEARCH_IN_CONTENT",
            }:
                raise ClientError(f"不支持的搜索范围: {search_in}")
            params: dict[str, Any] = {
                "q": query,
                "limit": limit,
                "search_in": search_in,
            }
            if cursor is not None:
                params["cursor"] = cursor
            payload = await self._request_json(
                "GET",
                f"/v1/aliases/{alias}/messages/search",
                "搜索邮件",
                params=params,
            )
            page = Page.from_payload(payload, MessageSummary.from_payload)
            logger.debug(
                f"[agently] 客户端操作完成 操作=search 账号={self.account} "
                f"地址ID={alias} 数量={len(page.data)} 还有更多={page.has_more} "
                f"下一游标存在={bool(page.next_cursor)}"
            )
            return page
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=search 账号={self.account} "
                f"异常={type(exc).__name__}"
            )
            raise

    async def send(
        self,
        *,
        to: Sequence[str | Sender | Mapping[str, Any]],
        subject: str,
        body: str,
        body_format: str,
        alias_id: str | None = None,
        cc: Sequence[str | Sender | Mapping[str, Any]] | None = None,
        bcc: Sequence[str | Sender | Mapping[str, Any]] | None = None,
        attachments: Sequence[Attachment | Mapping[str, Any]] | None = None,
    ) -> Mapping[str, Any]:
        """按确认协议发送邮件。"""
        logger.debug(
            f"[agently] 客户端操作开始 操作=send 账号={self.account} "
            f"地址ID={alias_id} 收件人数量={len(to)} 抄送数量={len(cc or [])} "
            f"密送数量={len(bcc or [])} 附件数量={len(attachments or [])}"
        )
        try:
            alias = await self._resolve_alias(alias_id)
            payload = {
                "to": self._recipient_payload(to, "to"),
                "cc": self._recipient_payload(cc or [], "cc"),
                "bcc": self._recipient_payload(bcc or [], "bcc"),
                "subject": subject,
                "body": body,
                "body_format": self._body_format(body_format),
                "attachments": self._attachment_payloads(attachments or []),
            }
            result = await self._confirmed_post(
                f"/v1/aliases/{alias}/messages/send",
                payload,
                "发送邮件",
            )
            logger.debug(
                f"[agently] 客户端操作完成 操作=send 账号={self.account} "
                f"地址ID={alias} 附件数量={len(attachments or [])}"
            )
            return result
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=send 账号={self.account} "
                f"异常={type(exc).__name__}"
            )
            raise

    async def reply(
        self,
        message_id: str,
        *,
        body: str,
        body_format: str,
        reply_all: bool,
        alias_id: str | None = None,
    ) -> Mapping[str, Any]:
        """按确认协议回复指定邮件。"""
        logger.debug(
            f"[agently] 客户端操作开始 操作=reply 账号={self.account} "
            f"地址ID={alias_id} 邮件ID={message_id} 回复全部={reply_all}"
        )
        try:
            alias = await self._resolve_alias(alias_id)
            self._require_identifier(message_id, "message_id")
            if not isinstance(reply_all, bool):
                raise ClientError("reply_all 必须是布尔值")
            payload = {
                "body": body,
                "body_format": self._body_format(body_format),
                "reply_all": reply_all,
            }
            result = await self._confirmed_post(
                f"/v1/aliases/{alias}/messages/{message_id}/reply",
                payload,
                "回复邮件",
            )
            logger.debug(
                f"[agently] 客户端操作完成 操作=reply 账号={self.account} "
                f"地址ID={alias} 邮件ID={message_id}"
            )
            return result
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=reply 账号={self.account} "
                f"邮件ID={message_id} 异常={type(exc).__name__}"
            )
            raise

    async def forward(
        self,
        message_id: str,
        *,
        to: Sequence[str | Sender | Mapping[str, Any]],
        body: str,
        body_format: str,
        include_attachments: bool,
        alias_id: str | None = None,
    ) -> Mapping[str, Any]:
        """按确认协议转发指定邮件。"""
        logger.debug(
            f"[agently] 客户端操作开始 操作=forward 账号={self.account} "
            f"地址ID={alias_id} 邮件ID={message_id} 收件人数量={len(to)} "
            f"包含附件={include_attachments}"
        )
        try:
            alias = await self._resolve_alias(alias_id)
            self._require_identifier(message_id, "message_id")
            if not isinstance(include_attachments, bool):
                raise ClientError("include_attachments 必须是布尔值")
            payload = {
                "to": self._recipient_payload(to, "to"),
                "body": body,
                "body_format": self._body_format(body_format),
                "include_attachments": include_attachments,
            }
            result = await self._confirmed_post(
                f"/v1/aliases/{alias}/messages/{message_id}/forward",
                payload,
                "转发邮件",
            )
            logger.debug(
                f"[agently] 客户端操作完成 操作=forward 账号={self.account} "
                f"地址ID={alias} 邮件ID={message_id}"
            )
            return result
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=forward 账号={self.account} "
                f"邮件ID={message_id} 异常={type(exc).__name__}"
            )
            raise

    async def trash(
        self,
        message_id: str,
        *,
        alias_id: str | None = None,
    ) -> Mapping[str, Any]:
        """将指定邮件移入垃圾箱。"""
        logger.debug(
            f"[agently] 客户端操作开始 操作=trash 账号={self.account} "
            f"地址ID={alias_id} 邮件ID={message_id}"
        )
        try:
            result = await self._delete_message(message_id, alias_id, "进垃圾箱", "")
            logger.debug(
                f"[agently] 客户端操作完成 操作=trash 账号={self.account} "
                f"邮件ID={message_id}"
            )
            return result
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=trash 账号={self.account} "
                f"邮件ID={message_id} 异常={type(exc).__name__}"
            )
            raise

    async def permanent_delete(
        self,
        message_id: str,
        *,
        alias_id: str | None = None,
    ) -> Mapping[str, Any]:
        """永久删除指定邮件。"""
        logger.debug(
            f"[agently] 客户端操作开始 操作=permanent_delete 账号={self.account} "
            f"地址ID={alias_id} 邮件ID={message_id}"
        )
        try:
            result = await self._delete_message(
                message_id,
                alias_id,
                "永久删除邮件",
                "/permanent",
            )
            logger.debug(
                f"[agently] 客户端操作完成 操作=permanent_delete "
                f"账号={self.account} 邮件ID={message_id}"
            )
            return result
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=permanent_delete "
                f"账号={self.account} 邮件ID={message_id} 异常={type(exc).__name__}"
            )
            raise

    async def empty_trash(self, *, alias_id: str | None = None) -> Mapping[str, Any]:
        """永久清空指定别名的垃圾箱。"""
        logger.debug(
            f"[agently] 客户端操作开始 操作=empty_trash 账号={self.account} "
            f"地址ID={alias_id}"
        )
        try:
            alias = await self._resolve_alias(alias_id)
            result = await self._request_json(
                "DELETE",
                f"/v1/aliases/{alias}/dir/trash/messages/permanent",
                "清空垃圾箱",
            )
            logger.debug(
                f"[agently] 客户端操作完成 操作=empty_trash "
                f"账号={self.account} 地址ID={alias}"
            )
            return result
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=empty_trash "
                f"账号={self.account} 异常={type(exc).__name__}"
            )
            raise

    async def events_wait(
        self,
        *,
        alias_id: str | None = None,
        next_cursor: str | None = None,
    ) -> Mapping[str, Any]:
        """执行一次 events/wait 长轮询请求。"""
        logger.debug(
            f"[agently] 客户端操作开始 操作=events_wait 账号={self.account} "
            f"地址ID={alias_id} 游标存在={next_cursor is not None}"
        )
        try:
            alias = await self._resolve_alias(alias_id)
            params: dict[str, str] = {}
            if next_cursor is not None:
                params["next_cursor"] = next_cursor
            response = await self._request(
                "GET",
                f"/v1/aliases/{alias}/events/wait",
                params=params or None,
            )
            payload = await self._response_json(response, "等待邮件事件")
            if response.is_error:
                await self._raise_response(response, "等待邮件事件", payload)
            events = payload.get("events")
            event_count = len(events) if isinstance(events, list) else None
            logger.debug(
                f"[agently] 客户端操作完成 操作=events_wait "
                f"账号={self.account} 地址ID={alias} 事件数量={event_count} "
                f"下一游标存在={bool(payload.get('next_cursor'))}"
            )
            return payload
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=events_wait "
                f"账号={self.account} 异常={type(exc).__name__}"
            )
            raise

    async def upload(
        self,
        filename: str,
        content_type: str,
        content: bytes,
        *,
        alias_id: str | None = None,
    ) -> Mapping[str, Any]:
        """按协议上传一个 Base64 编码附件。"""
        logger.debug(
            f"[agently] 客户端操作开始 操作=upload 账号={self.account} "
            f"地址ID={alias_id} 字节数={len(content) if isinstance(content, bytes) else -1}"
        )
        try:
            alias = await self._resolve_alias(alias_id)
            if not isinstance(content, bytes):
                raise ClientError("附件 content 必须是 bytes")
            encoded = base64.b64encode(content).decode("ascii")
            payload = {
                "filename": filename,
                "content_type": content_type,
                "size": len(content),
                "sha1": hashlib.sha1(content).hexdigest(),
                "content": encoded,
            }
            result = await self._request_json(
                "POST",
                f"/v1/aliases/{alias}/files/upload",
                "上传附件",
                json=payload,
            )
            logger.debug(
                f"[agently] 客户端操作完成 操作=upload 账号={self.account} "
                f"地址ID={alias} 字节数={len(content)}"
            )
            return result
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=upload 账号={self.account} "
                f"异常={type(exc).__name__}"
            )
            raise

    async def download(
        self,
        message_id: str,
        attachment_id: str | None = None,
        *,
        alias_id: str | None = None,
        download_url: str | None = None,
    ) -> bytes:
        """下载指定附件或读信响应提供的 download_url。"""
        logger.debug(
            f"[agently] 客户端操作开始 操作=download 账号={self.account} "
            f"地址ID={alias_id} 邮件ID={message_id} 附件ID={attachment_id} "
            f"来源={'download_url' if download_url is not None else 'api'}"
        )
        try:
            self._require_identifier(message_id, "message_id")
            if download_url is not None:
                _validate_download_url(download_url)
                endpoint = _safe_endpoint(download_url)
                started = time.perf_counter()
                # download_url 是可直接访问的下载地址，不带 Bearer 且不跟随重定向，避免凭证泄露或 SSRF。
                try:
                    async with self._http.stream(
                        "GET",
                        download_url,
                        headers={"User-Agent": USER_AGENT},
                        follow_redirects=False,
                    ) as response:
                        logger.debug(
                            f"[agently] 客户端下载 HTTP 完成 账号={self.account} "
                            f"地址={endpoint} 状态码={response.status_code} "
                            f"耗时={time.perf_counter() - started:.3f}s"
                        )
                        if response.is_redirect:
                            raise ClientError(
                                "下载附件失败: 拒绝未受信任的下载地址重定向",
                                status_code=response.status_code,
                            )
                        if response.is_error:
                            await response.aread()
                            await self._raise_response(response, "下载附件")
                        chunks = bytearray()
                        async for chunk in response.aiter_bytes():
                            chunks.extend(chunk)
                            if len(chunks) > MAX_DOWNLOAD_BYTES:
                                raise ClientError("下载附件失败: 附件体积超过 25MB 安全上限")
                        data = bytes(chunks)
                except httpx.HTTPError as exc:
                    logger.warning(
                        f"[agently] 客户端下载传输失败 账号={self.account} "
                        f"地址={endpoint} 异常={type(exc).__name__}"
                    )
                    raise ClientError("下载附件失败: 网络请求失败") from exc
                logger.debug(
                    f"[agently] 客户端操作完成 操作=download 账号={self.account} "
                    f"邮件ID={message_id} 字节数={len(data)}"
                )
                return data
            else:
                alias = await self._resolve_alias(alias_id)
                self._require_identifier(attachment_id, "attachment_id")
                url = f"/v1/aliases/{alias}/messages/{message_id}/attachments/{attachment_id}"
                response = await self._request("GET", url)
            if response.is_error:
                await self._raise_response(response, "下载附件")
            if len(response.content) > MAX_DOWNLOAD_BYTES:
                raise ClientError("下载附件失败: 附件体积超过 25MB 安全上限")
            logger.debug(
                f"[agently] 客户端操作完成 操作=download 账号={self.account} "
                f"邮件ID={message_id} 字节数={len(response.content)}"
            )
            return response.content
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=download 账号={self.account} "
                f"邮件ID={message_id} 异常={type(exc).__name__}"
            )
            raise

    async def get_account(self) -> Mapping[str, Any]:
        """通过 getaccount 读取账号昵称与别名配额。"""
        logger.debug(f"[agently] 客户端操作开始 操作=get_account 账号={self.account}")
        try:
            result = await self._mgr_post(GETACCOUNT_URL, {}, "读取账号信息")
            logger.debug(
                f"[agently] 客户端操作完成 操作=get_account "
                f"账号={self.account} 字段数量={len(result)}"
            )
            return result
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=get_account "
                f"账号={self.account} 异常={type(exc).__name__}"
            )
            raise

    async def suggest_address(self) -> Mapping[str, Any]:
        """获取服务端默认推荐的邮箱地址（按微信账号生成，或返回 90 天内待复用旧地址）。"""
        logger.debug(
            f"[agently] 客户端操作开始 操作=suggest_address 账号={self.account}"
        )
        try:
            created = await self._mgr_post(MGR_URL, {"func": "7"}, "获取推荐邮箱地址")
            alias = created.get("alias")
            if not isinstance(alias, str) or not alias:
                raise ClientError("获取推荐邮箱地址响应缺少 alias", details=created)
            result = {
                "alias": alias,
                "prefix": alias.split("@", 1)[0],
                "is_reused": bool(created.get("is_reused", False)),
            }
            logger.debug(
                f"[agently] 客户端操作完成 操作=suggest_address "
                f"账号={self.account} 是否复用={result['is_reused']}"
            )
            return result
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=suggest_address "
                f"账号={self.account} 异常={type(exc).__name__}"
            )
            raise

    async def check_address(self, prefix: str) -> Mapping[str, Any]:
        """检查自定义邮箱地址前缀是否可用。"""
        logger.debug(
            f"[agently] 客户端操作开始 操作=check_address 账号={self.account}"
        )
        try:
            new_alias = normalize_alias_email(prefix)
            body = await self._mgr_post(
                MGR_URL, {"func": "8", "new_alias": new_alias}, "检查邮箱地址"
            )
            result = {
                "alias": new_alias,
                "available": bool(body.get("alias_available", False)),
            }
            logger.debug(
                f"[agently] 客户端操作完成 操作=check_address "
                f"账号={self.account} 可用={result['available']}"
            )
            return result
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=check_address "
                f"账号={self.account} 异常={type(exc).__name__}"
            )
            raise

    async def create_address(self, prefix: str | None = None) -> Mapping[str, Any]:
        """新建邮箱地址：支持自定义前缀，缺省时由 func=7 获取默认前缀再 func=2 绑定。"""
        custom_prefix = prefix.strip() if isinstance(prefix, str) and prefix.strip() else None
        logger.debug(
            f"[agently] 客户端操作开始 操作=create_address 账号={self.account} "
            f"自定义前缀={custom_prefix is not None}"
        )
        try:
            is_reused = False
            if custom_prefix is not None:
                alias = normalize_alias_email(custom_prefix)
            else:
                created = await self._mgr_post(MGR_URL, {"func": "7"}, "创建邮箱地址")
                raw_alias = created.get("alias")
                if not isinstance(raw_alias, str) or not raw_alias:
                    raise ClientError("创建邮箱地址响应缺少 alias", details=created)
                alias = raw_alias
                is_reused = bool(created.get("is_reused", False))
            bound = await self._mgr_post(
                MGR_URL, {"func": "2", "new_alias": alias}, "绑定邮箱地址"
            )
            alias_id = bound.get("alias_id")
            if not isinstance(alias_id, str) or not alias_id:
                raise ClientError("绑定邮箱地址响应缺少 alias_id", details=bound)
            bound_alias = bound.get("alias")
            final_alias = (
                bound_alias if isinstance(bound_alias, str) and bound_alias else alias
            )
            result = {
                "alias": final_alias,
                "alias_id": alias_id,
                "is_reused": is_reused or bool(bound.get("is_reused", False)),
            }
            logger.debug(
                f"[agently] 客户端操作完成 操作=create_address "
                f"账号={self.account} 地址ID={alias_id} "
                f"是否复用={bool(result['is_reused'])}"
            )
            return result
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=create_address "
                f"账号={self.account} 异常={type(exc).__name__}"
            )
            raise

    async def modify_address(self, alias_id: str, prefix: str) -> Mapping[str, Any]:
        """修改已有邮箱地址的前缀（func=9）。"""
        logger.debug(
            f"[agently] 客户端操作开始 操作=modify_address 账号={self.account} "
            f"地址ID={alias_id}"
        )
        try:
            self._require_identifier(alias_id, "alias_id")
            new_alias = normalize_alias_email(prefix)
            body = await self._mgr_post(
                MGR_URL,
                {"func": "9", "alias_id": alias_id, "new_alias": new_alias},
                "修改邮箱地址",
            )
            updated_alias = body.get("alias")
            final_alias = (
                updated_alias
                if isinstance(updated_alias, str) and updated_alias
                else new_alias
            )
            self._me = None
            result = {
                "alias_id": alias_id,
                "alias": final_alias,
            }
            logger.debug(
                f"[agently] 客户端操作完成 操作=modify_address "
                f"账号={self.account} 地址ID={alias_id}"
            )
            return result
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=modify_address "
                f"账号={self.account} 地址ID={alias_id} 异常={type(exc).__name__}"
            )
            raise

    async def delete_address(self, alias_id: str) -> Mapping[str, Any]:
        """注销指定邮箱地址，注销不可恢复且地址保留 90 天供复用。"""
        logger.debug(
            f"[agently] 客户端操作开始 操作=delete_address 账号={self.account} "
            f"地址ID={alias_id}"
        )
        try:
            self._require_identifier(alias_id, "alias_id")
            await self._mgr_post(
                MGR_URL, {"func": "3", "alias_id": alias_id}, "注销邮箱地址"
            )
            result = {"alias_id": alias_id}
            logger.debug(
                f"[agently] 客户端操作完成 操作=delete_address "
                f"账号={self.account} 地址ID={alias_id}"
            )
            return result
        except Exception as exc:
            logger.debug(
                f"[agently] 客户端操作失败 操作=delete_address "
                f"账号={self.account} 地址ID={alias_id} 异常={type(exc).__name__}"
            )
            raise

    async def _delete_message(
        self,
        message_id: str,
        alias_id: str | None,
        operation: str,
        suffix: str,
    ) -> Mapping[str, Any]:
        alias = await self._resolve_alias(alias_id)
        self._require_identifier(message_id, "message_id")
        return await self._request_json(
            "DELETE",
            f"/v1/aliases/{alias}/messages/{message_id}{suffix}",
            operation,
        )

    async def _confirmed_post(
        self,
        path: str,
        payload: Mapping[str, Any],
        operation: str,
    ) -> Mapping[str, Any]:
        first = await self._request("POST", path, json=dict(payload))
        first_payload = await self._response_json(first, operation)
        if first.is_success:
            result = self._unwrap_data(first_payload)
            logger.debug(
                f"[agently] 客户端确认写操作完成 操作={operation} "
                f"账号={self.account} 需要确认=False"
            )
            return result
        code, details, message = self._error_parts(first_payload)
        if code != "CONFIRMATION_REQUIRED":
            raise ClientError(
                f"{operation}失败: {message}",
                status_code=first.status_code,
                code=code,
                details=details,
            )
        confirmation_token = details.get("confirmation_token") if isinstance(details, Mapping) else None
        if not isinstance(confirmation_token, str) or not confirmation_token:
            raise ClientError(
                f"{operation}需要确认但响应缺少 confirmation_token",
                status_code=first.status_code,
                code=code,
                details=details,
            )
        logger.debug(
            f"[agently] 客户端写操作需要确认 操作={operation} 账号={self.account}"
        )
        confirmed_payload = {**payload, "confirmation_token": confirmation_token}
        second = await self._request("POST", path, json=confirmed_payload)
        second_payload = await self._response_json(second, operation)
        if second.is_error:
            await self._raise_response(second, operation, second_payload)
        result = self._unwrap_data(second_payload)
        logger.debug(
            f"[agently] 客户端确认写操作完成 操作={operation} "
            f"账号={self.account} 需要确认=True"
        )
        return result

    async def _mgr_post(
        self,
        url: str,
        params: Mapping[str, str],
        operation: str,
    ) -> Mapping[str, Any]:
        """向 agent.qq.com 管理端点发表单请求，校验 head.ret 后返回 body。"""
        func = params.get("func")
        logger.debug(
            f"[agently] 客户端管理请求开始 操作={operation} 账号={self.account} "
            f"地址={_safe_endpoint(url)} 功能={func}"
        )
        form = {**params, "language": "zh", "r": secrets.token_hex(8)}
        headers = {
            "Origin": "https://agent.qq.com",
            "Referer": "https://agent.qq.com/page/setting",
        }
        response = await self._request("POST", url, data=form, headers=headers)
        payload = await self._response_json(response, operation)
        head = payload.get("head")
        if not isinstance(head, Mapping):
            logger.debug(
                f"[agently] 客户端管理响应缺少 head 操作={operation} "
                f"状态码={response.status_code}"
            )
            raise ClientError(
                f"{operation}响应缺少 head",
                status_code=response.status_code,
                details=payload,
            )
        ret = head.get("ret")
        if response.is_error or isinstance(ret, bool) or ret not in (0, "0"):
            ret_code = str(ret)
            msg = head.get("msg")
            mapped = _MGR_ERROR_MESSAGES.get(ret_code)
            message = (
                msg
                if isinstance(msg, str) and msg
                else (mapped if mapped is not None else f"ret={ret}")
            )
            if ret_code == "-20002":
                message = f"登录态失效（{message}）"
            logger.debug(
                f"[agently] 客户端管理响应失败 操作={operation} "
                f"状态码={response.status_code} 返回码={ret}"
            )
            raise ClientError(
                f"{operation}失败: {message}",
                status_code=response.status_code,
                code=ret_code,
                details=payload,
            )
        body = payload.get("body")
        if not isinstance(body, Mapping):
            logger.debug(
                f"[agently] 客户端管理响应缺少 body 操作={operation} "
                f"状态码={response.status_code}"
            )
            raise ClientError(
                f"{operation}响应缺少 body",
                status_code=response.status_code,
                details=payload,
            )
        logger.debug(
            f"[agently] 客户端管理请求完成 操作={operation} "
            f"账号={self.account} 返回码={ret} 字段数量={len(body)}"
        )
        return body

    async def _request_json(
        self,
        method: str,
        path: str,
        operation: str,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        response = await self._request(method, path, **kwargs)
        payload = await self._response_json(response, operation)
        if response.is_error:
            await self._raise_response(response, operation, payload)
        return self._unwrap_data(payload)

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        endpoint = _safe_endpoint(path)
        base_headers = dict(kwargs.pop("headers", {}))
        refreshed = False
        rate_attempt = 0
        request_attempt = 0
        while True:
            request_attempt += 1
            started = time.perf_counter()
            logger.debug(
                f"[agently] 客户端 HTTP 开始 账号={self.account} 方法={method} "
                f"地址={endpoint} 请求次数={request_attempt} 已刷新={refreshed} "
                f"限流重试次数={rate_attempt}"
            )
            bundle = await self.token_store.load(self.account)
            if bundle is None:
                logger.debug(f"[agently] 客户端请求无可用凭证 账号={self.account}")
                raise ClientError(f"账号 {self.account!r} 没有可用凭证")
            headers = dict(base_headers)
            headers["User-Agent"] = USER_AGENT
            headers["Authorization"] = f"Bearer {bundle.access_token}"
            try:
                response = await self._http.request(
                    method,
                    path,
                    headers=headers,
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    f"[agently] 客户端 HTTP 传输失败 账号={self.account} "
                    f"方法={method} 地址={endpoint} 请求次数={request_attempt} "
                    f"耗时={time.perf_counter() - started:.3f}s "
                    f"异常={type(exc).__name__}"
                )
                raise ClientError(f"请求 {method} {endpoint} 失败") from exc
            logger.debug(
                f"[agently] 客户端 HTTP 响应 账号={self.account} 方法={method} "
                f"地址={endpoint} 请求次数={request_attempt} "
                f"状态码={response.status_code} 耗时={time.perf_counter() - started:.3f}s"
            )
            if response.status_code == 401 and not refreshed:
                logger.debug(f"[agently] 客户端收到 401，开始刷新凭证 账号={self.account}")
                try:
                    await self._oauth.refresh()
                except Exception as exc:
                    logger.warning(
                        f"[agently] 客户端 401 凭证刷新失败 账号={self.account} "
                        f"异常={type(exc).__name__}"
                    )
                    raise
                refreshed = True
                logger.debug(f"[agently] 客户端凭证刷新完成，准备重试 账号={self.account}")
                continue
            if response.status_code == 429:
                delay = min(2.0**rate_attempt, 900.0)
                rate_attempt += 1
                logger.debug(
                    f"[agently] 客户端收到 429，准备重试 账号={self.account} "
                    f"限流重试次数={rate_attempt} 延迟={delay:.1f}s"
                )
                await self._sleep(delay)
                continue
            return response

    async def _resolve_alias(self, alias_id: str | None) -> str:
        if alias_id is not None:
            self._require_identifier(alias_id, "alias_id")
            return alias_id
        if self._alias_id is None:
            me = await self.me()
            primary = next((alias for alias in me.aliases if alias.is_primary), None)
            if primary is None:
                raise ClientError("/v1/me 未返回 is_primary 别名")
            self._alias_id = primary.alias_id
        return self._alias_id

    @staticmethod
    def _data_mapping(payload: Mapping[str, Any], operation: str) -> Mapping[str, Any]:
        data = payload.get("data", payload)
        if not isinstance(data, Mapping):
            raise ClientError(f"{operation}响应的 data 不是 JSON 对象")
        return data

    @staticmethod
    def _unwrap_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        data = payload.get("data")
        return data if isinstance(data, Mapping) else payload

    @staticmethod
    async def _response_json(
        response: httpx.Response,
        operation: str,
    ) -> Mapping[str, Any]:
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            logger.debug(
                f"[agently] 客户端响应非 JSON 操作={operation} "
                f"状态码={response.status_code}"
            )
            raise ClientError(
                f"{operation}返回非 JSON，HTTP {response.status_code}",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, Mapping):
            logger.debug(
                f"[agently] 客户端响应不是对象 操作={operation} "
                f"状态码={response.status_code}"
            )
            raise ClientError(
                f"{operation}返回的 JSON 不是对象",
                status_code=response.status_code,
            )
        return payload

    @staticmethod
    async def _raise_response(
        response: httpx.Response,
        operation: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        if payload is None:
            payload = await AgentlyMailClient._response_json(response, operation)
        code, details, message = AgentlyMailClient._error_parts(payload)
        logger.debug(
            f"[agently] 客户端响应业务失败 操作={operation} "
            f"状态码={response.status_code} 代码={code}"
        )
        raise ClientError(
            f"{operation}失败: {message}",
            status_code=response.status_code,
            code=code,
            details=details,
        )

    @staticmethod
    def _error_parts(payload: Mapping[str, Any]) -> tuple[str | None, Any, str]:
        error = payload.get("error")
        details: Any = None
        message = payload.get("message")
        code: str | None = None
        if isinstance(error, Mapping):
            code_value = error.get("code")
            code = code_value if isinstance(code_value, str) else None
            details = error.get("details")
            error_message = error.get("message")
            if isinstance(error_message, str):
                message = error_message
        elif isinstance(error, str):
            code = error
        if not isinstance(message, str) or not message:
            message = "服务器返回错误"
        return code, details, message

    @staticmethod
    def _recipient_payload(
        recipients: Iterable[str | Sender | Mapping[str, Any]],
        field: str,
    ) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for index, recipient in enumerate(recipients):
            if isinstance(recipient, Sender):
                email = recipient.email
            elif isinstance(recipient, str):
                email = recipient
            elif isinstance(recipient, Mapping):
                email = recipient.get("email")
            else:
                raise ClientError(f"{field}[{index}] 不是有效收件人")
            if not isinstance(email, str) or not email:
                raise ClientError(f"{field}[{index}] 缺少 email")
            result.append({"email": email})
        return result

    @staticmethod
    def _attachment_payloads(
        attachments: Iterable[Attachment | Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        allowed = ("filename", "content_type", "size", "sha1", "content")
        result: list[dict[str, Any]] = []
        for index, attachment in enumerate(attachments):
            if isinstance(attachment, Attachment):
                source = {
                    "filename": attachment.filename,
                    "content_type": attachment.content_type,
                    "size": attachment.size,
                    "sha1": attachment.sha1,
                    "content": attachment.content,
                }
            elif isinstance(attachment, Mapping):
                source = dict(attachment)
            else:
                raise ClientError(f"attachments[{index}] 不是有效附件")
            item = {
                key: source[key]
                for key in allowed
                if key in source and source[key] is not None
            }
            if not item:
                raise ClientError(f"attachments[{index}] 没有协议允许的字段")
            result.append(item)
        return result

    @staticmethod
    def _body_format(body_format: str) -> str:
        if body_format not in {"HTML", "PLAIN", "MARKDOWN"}:
            raise ClientError(f"不支持的 body_format: {body_format}")
        return body_format

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ClientError("limit 必须是 1 到 50 的整数")

    @staticmethod
    def _require_identifier(value: str | None, name: str) -> None:
        if not isinstance(value, str) or not value:
            raise ClientError(f"{name} 不能为空")
        if value in {".", ".."} or any(ord(char) < 32 or char in "/\\?#%" for char in value):
            raise ClientError(f"{name} 含非法路径字符")
