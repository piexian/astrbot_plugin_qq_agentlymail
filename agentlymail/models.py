"""定义协议响应对应的数据类。"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Callable, Generic, Mapping, TypeVar


T = TypeVar("T")


def _as_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} 应为 JSON 对象，实际为 {type(value).__name__}")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"文本字段应为字符串，实际为 {type(value).__name__}")
    return value


def _optional_int(value: Any, context: str) -> int | None:
    """兼容 protojson 把 int64 序列化为十进制字符串的场景。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{context} 应为整数")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        raise ValueError(f"{context} 应为整数")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                parsed = float(text)
            except ValueError:
                return None
            if math.isfinite(parsed) and parsed.is_integer():
                return int(parsed)
            return None
    raise ValueError(f"{context} 应为整数")


def _message_id(payload: Mapping[str, Any]) -> str:
    value = payload.get("message_id", payload.get("id"))
    if not isinstance(value, str) or not value:
        raise ValueError("消息响应缺少 message_id")
    return value


def _sender(value: Any, context: str) -> Sender | None:
    if value is None:
        return None
    if isinstance(value, str):
        return Sender(email=value)
    payload = _as_mapping(value, context)
    email = payload.get("email")
    if not isinstance(email, str) or not email:
        raise ValueError(f"{context} 缺少 email")
    return Sender(email=email, name=_optional_text(payload.get("name")))


def _senders(value: Any, context: str) -> list[Sender]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{context} 应为数组，实际为 {type(value).__name__}")
    result: list[Sender] = []
    for index, item in enumerate(value):
        parsed = _sender(item, f"{context}[{index}]")
        if parsed is None:
            raise ValueError(f"{context}[{index}] 不能为 null")
        result.append(parsed)
    return result


@dataclass(slots=True)
class Alias:
    """表示 /v1/me 返回的邮箱别名。"""

    alias_id: str
    is_primary: bool = False
    email: str | None = None

    @classmethod
    def from_payload(cls, value: Any) -> Alias:
        payload = _as_mapping(value, "alias")
        alias_id = payload.get("alias_id", payload.get("id"))
        if not isinstance(alias_id, str) or not alias_id:
            raise ValueError("alias 缺少 alias_id")
        is_primary = payload.get("is_primary", False)
        if not isinstance(is_primary, bool):
            raise ValueError("alias.is_primary 应为布尔值")
        return cls(
            alias_id=alias_id,
            is_primary=is_primary,
            email=_optional_text(payload.get("email")),
        )


@dataclass(slots=True)
class RateLimits:
    """保留 /v1/me 返回的限流对象原始键值。"""

    values: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, value: Any) -> RateLimits:
        payload = _as_mapping(value, "rate_limits")
        return cls(values=dict(payload))


@dataclass(slots=True)
class Constraints:
    """保留 /v1/me 返回的约束对象原始键值。"""

    values: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, value: Any) -> Constraints:
        payload = _as_mapping(value, "constraints")
        return cls(values=dict(payload))


@dataclass(slots=True)
class Sender:
    """表示邮件地址对象。"""

    email: str
    name: str | None = None


@dataclass(slots=True)
class Attachment:
    """表示邮件附件或附件上传内容。"""

    attachment_id: str | None = None
    filename: str | None = None
    content_type: str | None = None
    size: int | None = None
    sha1: str | None = None
    content: str | None = None
    download_url: str | None = None

    @classmethod
    def from_payload(cls, value: Any) -> Attachment:
        payload = _as_mapping(value, "attachment")
        attachment_id = payload.get("attachment_id", payload.get("id"))
        if attachment_id is not None and not isinstance(attachment_id, str):
            raise ValueError("attachment_id 应为字符串")
        size = _optional_int(payload.get("size"), "attachment.size")
        raw_filename = payload.get("filename")
        if raw_filename is None:
            raw_filename = payload.get("name")
        return cls(
            attachment_id=attachment_id,
            filename=_optional_text(raw_filename),
            content_type=_optional_text(payload.get("content_type")),
            size=size,
            sha1=_optional_text(payload.get("sha1")),
            content=_optional_text(payload.get("content")),
            download_url=_optional_text(payload.get("download_url")),
        )


@dataclass(slots=True)
class MessageSummary:
    """表示消息列表或搜索结果中的邮件摘要。"""

    message_id: str
    subject: str | None = None
    from_: Sender | None = None
    created_at: str | None = None
    is_read: bool | None = None

    @classmethod
    def from_payload(cls, value: Any) -> MessageSummary:
        payload = _as_mapping(value, "message summary")
        is_read = payload.get("is_read")
        if is_read is not None and not isinstance(is_read, bool):
            raise ValueError("message summary.is_read 应为布尔值")
        return cls(
            message_id=_message_id(payload),
            subject=_optional_text(payload.get("subject")),
            from_=_sender(payload.get("from"), "message summary.from"),
            created_at=_optional_text(payload.get("created_at")),
            is_read=is_read,
        )


@dataclass(slots=True)
class Message:
    """表示读信端点返回的完整邮件。"""

    message_id: str
    body: str
    body_format: str
    from_: Sender | None = None
    to: list[Sender] = field(default_factory=list)
    cc: list[Sender] = field(default_factory=list)
    bcc: list[Sender] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    created_at: str | None = None
    subject: str | None = None

    @classmethod
    def from_payload(cls, value: Any) -> Message:
        payload = _as_mapping(value, "message")
        body = payload.get("body")
        if body is None:
            body = ""
        elif not isinstance(body, str):
            raise ValueError("message 缺少字符串 body")
        body_format = payload.get("body_format")
        if body_format is None:
            body_format = "PLAIN"
        elif not isinstance(body_format, str):
            raise ValueError("message 缺少字符串 body_format")
        attachments = payload.get("attachments")
        if attachments is None:
            attachments = []
        elif not isinstance(attachments, list):
            raise ValueError("message.attachments 应为数组")
        return cls(
            message_id=_message_id(payload),
            subject=_optional_text(payload.get("subject")),
            body=body,
            body_format=body_format,
            from_=_sender(payload.get("from"), "message.from"),
            to=_senders(payload.get("to"), "message.to"),
            cc=_senders(payload.get("cc"), "message.cc"),
            bcc=_senders(payload.get("bcc"), "message.bcc"),
            attachments=[
                Attachment.from_payload(item)
                for item in attachments
            ],
            created_at=_optional_text(payload.get("created_at")),
        )


@dataclass(slots=True)
class Page(Generic[T]):
    """表示带 pagination 游标的分页响应。"""

    data: list[T] = field(default_factory=list)
    has_more: bool = False
    next_cursor: str | None = None

    @classmethod
    def from_payload(
        cls,
        value: Any,
        item_factory: Callable[[Any], T],
    ) -> Page[T]:
        payload = _as_mapping(value, "page")
        items = payload.get("data")
        if items is None:
            items = []
        elif not isinstance(items, list):
            raise ValueError("page.data 应为数组")
        raw_pagination = payload.get("pagination")
        pagination = _as_mapping({} if raw_pagination is None else raw_pagination, "page.pagination")
        has_more = pagination.get("has_more", False)
        if not isinstance(has_more, bool):
            raise ValueError("page.pagination.has_more 应为布尔值")
        next_cursor = _optional_text(pagination.get("next_cursor"))
        return cls(
            data=[item_factory(item) for item in items],
            has_more=has_more,
            next_cursor=next_cursor,
        )


@dataclass(slots=True)
class Me:
    """表示 /v1/me 返回的身份、别名、限流和约束信息。"""

    aliases: list[Alias] = field(default_factory=list)
    rate_limits: RateLimits = field(default_factory=RateLimits)
    constraints: Constraints = field(default_factory=Constraints)
    scopes: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, value: Any) -> Me:
        payload = _as_mapping(value, "me")
        aliases = payload.get("aliases")
        if aliases is None:
            aliases = []
        elif not isinstance(aliases, list):
            raise ValueError("me.aliases 应为数组")
        scopes = payload.get("scopes")
        if scopes is None:
            scopes = []
        elif not isinstance(scopes, list) or any(not isinstance(item, str) for item in scopes):
            raise ValueError("me.scopes 应为字符串数组")
        rate_limits = payload.get("rate_limits")
        constraints = payload.get("constraints")
        return cls(
            aliases=[Alias.from_payload(item) for item in aliases],
            rate_limits=RateLimits.from_payload({} if rate_limits is None else rate_limits),
            constraints=Constraints.from_payload({} if constraints is None else constraints),
            scopes=list(scopes),
        )
