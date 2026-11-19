"""封装 QQ Agently Mail 的认证请求和全部协议端点。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any

import httpx

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
        payload = await self._request_json("GET", "/v1/me", "读取身份")
        me = Me.from_payload(self._data_mapping(payload, "读取身份"))
        self._me = me
        self.rate_limits = me.rate_limits
        self.constraints = me.constraints
        if self._alias_id is None:
            primary = next((alias for alias in me.aliases if alias.is_primary), None)
            if primary is not None:
                self._alias_id = primary.alias_id
        return me

    async def list_messages(
        self,
        *,
        alias_id: str | None = None,
        limit: int = 50,
        direction: str = "inbox",
        cursor: str | None = None,
    ) -> Page[MessageSummary]:
        """分页读取指定别名的邮件列表。"""
        alias = await self._resolve_alias(alias_id)
        self._validate_limit(limit)
        if direction not in {"inbox", "sent", "trash", "spam"}:
            raise ClientError(f"不支持的邮件目录: {direction}")
        params: dict[str, Any] = {"limit": limit, "dir": direction}
        if cursor is not None:
            params["cursor"] = cursor
        payload = await self._request_json(
            "GET",
            f"/v1/aliases/{alias}/messages",
            "读取邮件列表",
            params=params,
        )
        return Page.from_payload(payload, MessageSummary.from_payload)

    async def read(
        self,
        message_id: str,
        *,
        alias_id: str | None = None,
    ) -> Message:
        """读取指定邮件的完整内容。"""
        alias = await self._resolve_alias(alias_id)
        self._require_identifier(message_id, "message_id")
        payload = await self._request_json(
            "GET",
            f"/v1/aliases/{alias}/messages/{message_id}",
            "读取邮件",
        )
        return Message.from_payload(self._data_mapping(payload, "读取邮件"))

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
        return Page.from_payload(payload, MessageSummary.from_payload)

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
        return await self._confirmed_post(
            f"/v1/aliases/{alias}/messages/send",
            payload,
            "发送邮件",
        )

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
        alias = await self._resolve_alias(alias_id)
        self._require_identifier(message_id, "message_id")
        if not isinstance(reply_all, bool):
            raise ClientError("reply_all 必须是布尔值")
        payload = {
            "body": body,
            "body_format": self._body_format(body_format),
            "reply_all": reply_all,
        }
        return await self._confirmed_post(
            f"/v1/aliases/{alias}/messages/{message_id}/reply",
            payload,
            "回复邮件",
        )

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
        return await self._confirmed_post(
            f"/v1/aliases/{alias}/messages/{message_id}/forward",
            payload,
            "转发邮件",
        )

    async def trash(
        self,
        message_id: str,
        *,
        alias_id: str | None = None,
    ) -> Mapping[str, Any]:
        """将指定邮件移入垃圾箱。"""
        return await self._delete_message(message_id, alias_id, "进垃圾箱", "")

    async def permanent_delete(
        self,
        message_id: str,
        *,
        alias_id: str | None = None,
    ) -> Mapping[str, Any]:
        """永久删除指定邮件。"""
        return await self._delete_message(
            message_id,
            alias_id,
            "永久删除邮件",
            "/permanent",
        )

    async def empty_trash(self, *, alias_id: str | None = None) -> Mapping[str, Any]:
        """永久清空指定别名的垃圾箱。"""
        alias = await self._resolve_alias(alias_id)
        return await self._request_json(
            "DELETE",
            f"/v1/aliases/{alias}/dir/trash/messages/permanent",
            "清空垃圾箱",
        )

    async def events_wait(
        self,
        *,
        alias_id: str | None = None,
        next_cursor: str | None = None,
    ) -> Mapping[str, Any]:
        """执行一次 events/wait 长轮询请求。"""
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
        return payload

    async def upload(
        self,
        filename: str,
        content_type: str,
        content: bytes,
        *,
        alias_id: str | None = None,
    ) -> Mapping[str, Any]:
        """按协议上传一个 Base64 编码附件。"""
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
        return await self._request_json(
            "POST",
            f"/v1/aliases/{alias}/files/upload",
            "上传附件",
            json=payload,
        )

    async def download(
        self,
        message_id: str,
        attachment_id: str | None = None,
        *,
        alias_id: str | None = None,
        download_url: str | None = None,
    ) -> bytes:
        """下载指定附件或读信响应提供的 download_url。"""
        self._require_identifier(message_id, "message_id")
        if download_url is not None:
            # download_url 是可直接访问的下载地址，不带 Bearer，避免凭证泄露给外部域。
            try:
                response = await self._http.request(
                    "GET", download_url, headers={"User-Agent": USER_AGENT}
                )
            except httpx.HTTPError as exc:
                raise ClientError(f"下载附件失败: {exc}") from exc
        else:
            alias = await self._resolve_alias(alias_id)
            self._require_identifier(attachment_id, "attachment_id")
            url = f"/v1/aliases/{alias}/messages/{message_id}/attachments/{attachment_id}"
            response = await self._request("GET", url)
        if response.is_error:
            await self._raise_response(response, "下载附件")
        return response.content

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
            return self._unwrap_data(first_payload)
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
        confirmed_payload = {**payload, "confirmation_token": confirmation_token}
        second = await self._request("POST", path, json=confirmed_payload)
        second_payload = await self._response_json(second, operation)
        if second.is_error:
            await self._raise_response(second, operation, second_payload)
        return self._unwrap_data(second_payload)

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
        refreshed = False
        rate_attempt = 0
        while True:
            bundle = await self.token_store.load(self.account)
            if bundle is None:
                raise ClientError(f"账号 {self.account!r} 没有可用凭证")
            headers = dict(kwargs.pop("headers", {}))
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
                raise ClientError(f"请求 {method} {path} 失败: {exc}") from exc
            if response.status_code == 401 and not refreshed:
                await self._oauth.refresh()
                refreshed = True
                continue
            if response.status_code == 429:
                delay = min(2.0**rate_attempt, 900.0)
                rate_attempt += 1
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
            raise ClientError(
                f"{operation}返回非 JSON，HTTP {response.status_code}",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, Mapping):
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
