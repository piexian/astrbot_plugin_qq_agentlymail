"""实现插件 WebUI 的真实 Web API，供 AstrBotPluginView bridge 调用。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from astrbot.api import logger
from astrbot.api.web import error_response, file_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .client import AgentlyMailClient, ClientError
from .headless import (
    Func3Result,
    HeadlessQRLogin,
    HeadlessQRLoginError,
    HeadlessQRSession,
)
from .models import Alias, Me, Message, MessageSummary, Page
from .oauth import (
    AccessDeniedError,
    AuthorizationPendingError,
    DeviceFlowStart,
    ExpiredTokenError,
    OAuthClient,
    OAuthError,
)
from .token_store import TokenBundle, TokenStoreError
from .tools import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_COUNT,
    MAX_TOTAL_ATTACHMENT_BYTES,
    _safe_path_identifier,
)

if TYPE_CHECKING:
    from ..main import QQAgentlyMailPlugin

ROUTE_PREFIX = "/astrbot_plugin_qq_agentlymail"

# 读请求 30 秒、写请求与下载 60 秒；client 的 429 退避最长 900 秒，必须用超时截断。
READ_TIMEOUT = 30.0
WRITE_TIMEOUT = 60.0
DOWNLOAD_TIMEOUT = 60.0

# 所有 POST 原始请求体上限 28MiB，先判长再解析；subject/body 上限取自协议观测约束。
MAX_BODY_BYTES = 28 * 1024 * 1024
MAX_SUBJECT_BYTES = 4096
MAX_BODY_TEXT_BYTES = 1024 * 1024
MAX_LOGIN_SESSIONS = 8
MAX_ACCOUNT_NAME_LEN = 64

T = TypeVar("T")


def _qr_data_url(image: bytes) -> str:
    """按文件头魔数把二维码字节编码为 Data URL，仅用于页面展示。"""
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif image.startswith(b"RIFF") and image[8:12] == b"WEBP":
        mime = "image/webp"
    elif image.startswith(b"GIF8"):
        mime = "image/gif"
    else:
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"


@dataclass(slots=True)
class _WebLoginSession:
    """保存单次 WebUI 扫码登录会话状态，敏感票据仅保留在服务端。"""

    session_id: str
    account: str
    mode: str
    flow: DeviceFlowStart
    deadline: float
    expires_at: float
    qr_image: str | None = None
    qr_expired: bool = False
    state: str = "waiting_scan"
    mails: list[dict[str, Any]] | None = None
    email: str | None = None
    error: str | None = None
    error_code: str | None = None
    headless: HeadlessQRLogin | None = field(default=None, repr=False)
    qr_session: HeadlessQRSession | None = field(default=None, repr=False)
    user_code: str | None = field(default=None, repr=False)
    func3_result: Func3Result | None = field(default=None, repr=False)
    mail_choice: asyncio.Future[str] | None = field(default=None, repr=False)
    task: asyncio.Task[Any] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """返回不含任何令牌、票据或轮询地址的安全状态快照。"""
        return {
            "session_id": self.session_id,
            "account": self.account,
            "mode": self.mode,
            "state": self.state,
            "qr_image": self.qr_image,
            "qr_expired": self.qr_expired,
            "browser_url": self.flow.browser_url,
            "input_code": self.flow.input_code,
            "expires_at": _iso(self.expires_at),
            "mails": self.mails,
            "email": self.email,
            "error": self.error,
            "error_code": self.error_code,
        }


class WebApiError(Exception):
    """表示 Web API 的参数或业务错误，携带对外 HTTP 状态码。"""

    def __init__(self, message: str, status_code: int = 400, data: Any = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.data = data


async def _timed(awaitable: Awaitable[T], seconds: float, *, write: bool = False) -> T:
    """给上游调用加超时；写操作超时结果未知，提示用户先核对状态。"""
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except asyncio.TimeoutError as exc:
        if write:
            raise WebApiError(
                "上游请求超时，操作结果未知，请检查已发送/邮箱状态后再试。", 504
            ) from exc
        raise WebApiError("上游请求超时，请稍后重试（可能正在限流重试）。", 504) from exc


def _client_error_response(exc: ClientError):
    """把协议客户端错误映射为安全 HTTP 响应，不泄漏 details 与下载地址。"""
    status = exc.status_code
    if status == 401 or exc.code == "-20002":
        return error_response("凭证失效，请在聊天中重新执行 /agently login。", status_code=401)
    if status == 404:
        return error_response("邮件或附件不存在。", status_code=404)
    if status in (400, 422) or exc.code in {"INVALID_ALIAS", "-75103", "-75104", "-75105"}:
        return error_response(
            str(exc), status_code=400, data={"code": exc.code} if exc.code else None
        )
    if status == 403:
        return error_response(str(exc), status_code=403)
    logger.warning(
        f"[agently] Web API 上游错误：异常={type(exc).__name__}，"
        f"代码={exc.code}，状态码={exc.status_code}"
    )
    return error_response("上游服务错误，请稍后重试。", status_code=502)


def _guarded(handler: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """统一捕获 handler 异常并转为安全错误响应。"""

    @functools.wraps(handler)
    async def wrapper(self: MailWebApi, *args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        name = handler.__name__
        method = request.method
        path = request.path
        logger.debug(f"[agently] Web 请求开始：处理器={name}，方法={method}，路径={path}")
        try:
            response = await handler(self, *args, **kwargs)
        except WebApiError as exc:
            logger.warning(
                f"[agently] Web 请求失败：处理器={name}，方法={method}，路径={path}，"
                f"异常={type(exc).__name__}，状态码={exc.status_code}，"
                f"耗时={time.perf_counter() - started:.3f}秒"
            )
            return error_response(exc.message, status_code=exc.status_code, data=exc.data)
        except ClientError as exc:
            logger.debug(
                f"[agently] Web 上游调用失败：处理器={name}，方法={method}，路径={path}，"
                f"代码={exc.code}，状态码={exc.status_code}，"
                f"耗时={time.perf_counter() - started:.3f}秒"
            )
            return _client_error_response(exc)
        except OAuthError as exc:
            logger.warning(
                f"[agently] Web 凭证错误：处理器={name}，方法={method}，路径={path}，"
                f"代码={exc.code}，耗时={time.perf_counter() - started:.3f}秒"
            )
            return error_response(
                "凭证失效，请在聊天中重新执行 /agently login。", status_code=401
            )
        except TokenStoreError:
            logger.exception(
                f"[agently] Web 凭证存储错误：处理器={name}，方法={method}，路径={path}，"
                f"耗时={time.perf_counter() - started:.3f}秒"
            )
            return error_response("凭证存储错误，详情见插件日志。", status_code=500)
        except Exception:
            logger.exception(
                f"[agently] Web 未预期错误：处理器={name}，方法={method}，路径={path}，"
                f"耗时={time.perf_counter() - started:.3f}秒"
            )
            return error_response("插件内部错误，详情见插件日志。", status_code=500)
        logger.debug(
            f"[agently] Web 请求成功：处理器={name}，方法={method}，路径={path}，"
            f"状态码={getattr(response, 'status', None)}，"
            f"耗时={time.perf_counter() - started:.3f}秒"
        )
        return response

    return wrapper


def _validate_identifier(value: str, name: str) -> None:
    """校验协议标识非空且不含路径与控制字符。"""
    if not _safe_path_identifier(value):
        raise WebApiError(f"参数 {name} 为空或含非法字符。")


def _validate_account_name(account: str) -> str:
    """按凭证存储的路径规则校验账号名。"""
    name = account.strip()
    if (
        not name
        or len(name) > MAX_ACCOUNT_NAME_LEN
        or name in {".", ".."}
        or any(c in name for c in ("\x00", "/", "\\"))
    ):
        raise WebApiError("账号名为空、过长或含非法路径字符。")
    return name


def _query_str(
    name: str, *, required: bool = True, default: str | None = None, max_len: int = 512
) -> str | None:
    """读取查询字符串参数，校验非空与长度。"""
    value = request.query.get(name)
    if value is None or not value.strip():
        if required:
            raise WebApiError(f"缺少参数 {name}。")
        return default
    value = value.strip()
    if len(value) > max_len:
        raise WebApiError(f"参数 {name} 超过 {max_len} 字符上限。")
    return value


def _query_int(name: str, *, default: int, lo: int, hi: int) -> int:
    """读取整数查询参数并校验范围。"""
    raw = request.query.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise WebApiError(f"参数 {name} 必须是整数。") from exc
    if not lo <= value <= hi:
        raise WebApiError(f"参数 {name} 必须在 {lo} 到 {hi} 之间。")
    return value


def _iso(timestamp: float) -> str:
    """把 Unix 时间戳转为本地时区 ISO8601 文本。"""
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def _alias_dict(alias: Alias) -> dict[str, Any]:
    return {
        "alias_id": alias.alias_id,
        "email": alias.email,
        "is_primary": alias.is_primary,
    }


def _sender_dict(sender: Any) -> dict[str, Any] | None:
    if sender is None:
        return None
    return {"email": sender.email, "name": sender.name}


def _summary_dict(summary: MessageSummary) -> dict[str, Any]:
    return {
        "message_id": summary.message_id,
        "subject": summary.subject,
        "from_": _sender_dict(summary.from_),
        "created_at": summary.created_at,
        "is_read": summary.is_read,
    }


def _attachment_meta(attachment: Any) -> dict[str, Any]:
    # 只暴露附件元数据，content 与 download_url 不出后端。
    return {
        "attachment_id": attachment.attachment_id,
        "filename": attachment.filename,
        "content_type": attachment.content_type,
        "size": attachment.size,
    }


def _message_dict(message: Message) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "subject": message.subject,
        "body": message.body,
        "body_format": message.body_format,
        "from_": _sender_dict(message.from_),
        "to": [_sender_dict(item) for item in message.to],
        "cc": [_sender_dict(item) for item in message.cc],
        "bcc": [_sender_dict(item) for item in message.bcc],
        "attachments": [_attachment_meta(item) for item in message.attachments],
        "created_at": message.created_at,
    }


def _page_dict(page: Page) -> dict[str, Any]:
    return {
        "data": [_summary_dict(item) for item in page.data],
        "has_more": page.has_more,
        "next_cursor": page.next_cursor,
    }


def _safe_filename(name: Any) -> str:
    """清洗下载文件名：去路径分隔符与控制字符，拒绝空名与 . / ..。"""
    if not isinstance(name, str):
        return "attachment"
    cleaned = "".join(
        char for char in name if char not in "/\\" and ord(char) >= 32 and ord(char) != 127
    )
    cleaned = cleaned.strip().strip(".").strip()
    if not cleaned or cleaned in {".", ".."}:
        return "attachment"
    return cleaned[:200]


async def _read_json_body() -> dict[str, Any]:
    """读取并解析 JSON 请求体，先按原始字节判长。"""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise WebApiError("请求体超过 28MiB 上限。", 413)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebApiError("请求体不是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise WebApiError("请求体必须是 JSON 对象。")
    return payload


def _body_str(payload: dict[str, Any], key: str) -> str:
    """取必填字符串字段并去空白。"""
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WebApiError(f"字段 {key} 必须是非空字符串。")
    return value.strip()


def _body_text(payload: dict[str, Any], key: str) -> str:
    """取必填补文字段，保留原始空白格式。"""
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WebApiError(f"字段 {key} 必须是非空字符串。")
    return value


def _body_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    """取可选布尔字段，缺省用默认值。"""
    value = payload.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise WebApiError(f"字段 {key} 必须是布尔值。")
    return value


def _recipients(payload: dict[str, Any], key: str, *, required: bool) -> list[str]:
    """取收件人字符串数组，逐项校验非空。"""
    value = payload.get(key)
    if value is None:
        if required:
            raise WebApiError(f"缺少字段 {key}。")
        return []
    if not isinstance(value, list):
        raise WebApiError(f"字段 {key} 必须是字符串数组。")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise WebApiError(f"{key}[{index}] 必须是非空邮箱字符串。")
        result.append(item.strip())
    if required and not result:
        raise WebApiError(f"字段 {key} 不能为空数组。")
    return result


def _check_utf8_len(value: str, limit: int, name: str) -> None:
    """按 UTF-8 字节数校验文本上限。"""
    if len(value.encode("utf-8")) > limit:
        raise WebApiError(f"字段 {name} 超过 {limit} 字节上限。")


def _build_attachments(value: Any) -> list[dict[str, Any]]:
    """把浏览器内联附件组装为协议五字段，size/sha1 由后端计算。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise WebApiError("attachments 必须是数组。")
    if len(value) > MAX_ATTACHMENT_COUNT:
        raise WebApiError(f"附件数量超过上限 {MAX_ATTACHMENT_COUNT} 个。")
    result: list[dict[str, Any]] = []
    total = 0
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise WebApiError(f"attachments[{index}] 必须是对象。")
        raw_name = item.get("filename")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise WebApiError(f"attachments[{index}].filename 不能为空。")
        filename = _safe_filename(raw_name)
        content_type = item.get("content_type") or "application/octet-stream"
        if not isinstance(content_type, str):
            raise WebApiError(f"attachments[{index}].content_type 必须是字符串。")
        content_b64 = item.get("content")
        if not isinstance(content_b64, str) or not content_b64:
            raise WebApiError(f"attachments[{index}].content 必须是非空 Base64 字符串。")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WebApiError(f"attachments[{index}].content 不是有效 Base64。") from exc
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise WebApiError(f"attachments[{index}] 超过单附件 20MB 上限。")
        total += len(content)
        if total > MAX_TOTAL_ATTACHMENT_BYTES:
            raise WebApiError("附件总大小超过 20MB 上限。")
        result.append(
            {
                "filename": filename,
                "content_type": content_type,
                "size": len(content),
                "sha1": hashlib.sha1(content).hexdigest(),
                "content": content_b64,
            }
        )
    return result

class MailWebApi:
    """承载 WebUI 的全部 Web API handler，复用插件的客户端与凭证存储。"""

    def __init__(self, plugin: QQAgentlyMailPlugin):
        self._plugin = plugin
        self._login_lock = asyncio.Lock()
        self._login_sessions: dict[str, _WebLoginSession] = {}
        self._active_login_id: str | None = None

    @staticmethod
    def _make_headless() -> HeadlessQRLogin:
        """创建无头扫码登录客户端。"""
        return HeadlessQRLogin()

    @staticmethod
    async def _close_headless(account: str, headless: HeadlessQRLogin) -> None:
        """关闭无头登录客户端，清理失败只记录日志。"""
        try:
            await headless.close()
        except Exception:
            logger.exception(f"[agently] Web 登录关闭无头客户端失败：账号={account}")

    def _prune_login_sessions(self) -> None:
        """裁剪历史已结束的登录会话，避免内存无限增长。"""
        if len(self._login_sessions) <= MAX_LOGIN_SESSIONS:
            return
        finished = [
            sid
            for sid, item in self._login_sessions.items()
            if sid != self._active_login_id and (item.task is None or item.task.done())
        ]
        while len(self._login_sessions) > MAX_LOGIN_SESSIONS and finished:
            self._login_sessions.pop(finished.pop(0), None)

    async def _cancel_session(self, session: _WebLoginSession) -> None:
        """取消单次 Web 登录任务并等待清理完成。"""
        task = session.task
        if session.mail_choice is not None and not session.mail_choice.done():
            session.mail_choice.cancel()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if session.headless is not None:
            headless = session.headless
            session.headless = None
            await self._close_headless(session.account, headless)
        if session.state not in {"success", "error", "expired"}:
            session.state = "cancelled"
            session.qr_image = None
            session.mails = None
        if self._active_login_id == session.session_id:
            self._active_login_id = None

    async def _run_qr_side(self, session: _WebLoginSession) -> None:
        """执行无头微信扫码轮询、邮箱查询、可选多邮箱选择与登录确认。"""
        headless = session.headless
        qr_session = session.qr_session
        user_code = session.user_code
        if headless is None or qr_session is None or not user_code:
            raise HeadlessQRLoginError("invalid_state", "缺少无头扫码会话参数")

        async def on_qr_status(status_code: int) -> None:
            if status_code == 404 and session.state == "waiting_scan":
                session.state = "scanned"
                logger.debug(
                    f"[agently] Web 登录已扫码等待确认：账号={session.account}，"
                    f"会话={session.session_id}"
                )

        wx_code = await headless.poll_wx_code(
            qr_session,
            session.deadline,
            on_status=on_qr_status,
        )
        func3_result = await headless.func3(user_code, wx_code, deadline=session.deadline)
        session.func3_result = func3_result

        if len(func3_result.mails) == 1:
            mail = func3_result.mails[0]
            if not mail.is_selected:
                raise HeadlessQRLoginError(
                    "invalid_response",
                    "唯一可用邮箱没有 is_selected 标记",
                )
            chosen_email = mail.email
        else:
            session.mails = [
                {"email": mail.email, "is_selected": mail.is_selected}
                for mail in func3_result.mails
            ]
            session.mail_choice = asyncio.get_running_loop().create_future()
            session.state = "select_mail"
            logger.debug(
                f"[agently] Web 登录等待多邮箱选择：账号={session.account}，"
                f"候选数量={len(func3_result.mails)}"
            )
            remaining = session.deadline - time.monotonic()
            if remaining <= 0:
                raise HeadlessQRLoginError("expired", "登录流程已超时")
            try:
                chosen_email = await asyncio.wait_for(session.mail_choice, timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise HeadlessQRLoginError("expired", "邮箱选择已超时") from exc

        session.state = "authorizing"
        await headless.func8(
            user_code,
            chosen_email,
            func3_result.login_tmpticket,
            deadline=session.deadline,
        )

    @staticmethod
    def _map_headless_error(exc: HeadlessQRLoginError) -> tuple[str, str, str]:
        """把无头登录错误映射为 (state, error_code, message)。"""
        if exc.code in {"expired", "402"}:
            return ("expired", "expired", "二维码或登录会话已过期，请刷新重试。")
        if exc.code == "403":
            return ("error", "wx_cancelled", "已在手机端取消登录授权。")
        if exc.code == "-601":
            return ("error", "wx_601", "微信登录返回 -601，系统失败或邮箱为空，请刷新重试。")
        if exc.code == "-75111":
            return ("error", "wx_75111", "微信登录返回 -75111，扫码链接已使用或流程不匹配，请刷新重试。")
        if exc.code == "-5012":
            return ("error", "wx_5012", "微信登录返回 -5012，登录票据已过期或已消费，请刷新重试。")
        if exc.code == "transport_error":
            return ("error", "transport_error", "微信扫码网络请求失败，请刷新重试或复制原链接授权。")
        return ("error", "headless_error", exc.message or "扫码登录失败，请刷新重试。")

    async def _run_login_session(
        self,
        session: _WebLoginSession,
        client: AgentlyMailClient,
        oauth: OAuthClient,
    ) -> None:
        """后台协同微信扫码与设备流轮询，任意一条链路完成授权即落盘凭证。"""
        oauth_task: asyncio.Task[TokenBundle] = asyncio.create_task(
            oauth.poll(session.flow, deadline=session.deadline)
        )
        qr_task: asyncio.Task[None] | None = (
            asyncio.create_task(self._run_qr_side(session))
            if session.mode == "qr"
            else None
        )
        try:
            if qr_task is None:
                await oauth_task
            else:
                done, _pending = await asyncio.wait(
                    {oauth_task, qr_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if oauth_task in done:
                    qr_task.cancel()
                    await asyncio.gather(qr_task, return_exceptions=True)
                    oauth_task.result()
                else:
                    qr_exc = qr_task.exception()
                    if qr_exc is None:
                        await oauth_task
                    elif (
                        isinstance(qr_exc, HeadlessQRLoginError)
                        and qr_exc.code in {"402", "expired"}
                        and session.deadline > time.monotonic()
                        and session.state in {"waiting_scan", "scanned"}
                    ):
                        # 微信二维码 2-3 分钟过期，但原设备流链接在 10 分钟内仍可完成授权。
                        session.qr_expired = True
                        session.qr_image = None
                        session.mode = "browser"
                        session.state = "waiting_scan"
                        logger.info(
                            f"[agently] Web 登录二维码过期，保留原链接监听：账号={session.account}"
                        )
                        await oauth_task
                    else:
                        oauth_task.cancel()
                        await asyncio.gather(oauth_task, return_exceptions=True)
                        raise qr_exc
        except asyncio.CancelledError:
            logger.debug(
                f"[agently] Web 登录后台任务已取消：账号={session.account}，"
                f"会话={session.session_id}"
            )
            if session.state not in {"success", "error", "expired"}:
                session.state = "cancelled"
            raise
        except AccessDeniedError:
            session.state = "error"
            session.error_code = "access_denied"
            session.error = "用户拒绝了设备授权。"
            logger.warning(f"[agently] Web 登录授权被拒绝：账号={session.account}")
        except (ExpiredTokenError, AuthorizationPendingError):
            session.state = "expired"
            session.error_code = "expired"
            session.error = "登录二维码或授权链接已过期，请刷新重试。"
            logger.warning(f"[agently] Web 登录授权已过期：账号={session.account}")
        except HeadlessQRLoginError as exc:
            state, code, message = self._map_headless_error(exc)
            session.state = state
            session.error_code = code
            session.error = message
            logger.warning(
                f"[agently] Web 无头登录失败：账号={session.account}，代码={exc.code}"
            )
        except OAuthError as exc:
            session.state = "error"
            session.error_code = "oauth_error"
            session.error = exc.message or "授权失败，请刷新重试。"
            logger.warning(
                f"[agently] Web OAuth 登录失败：账号={session.account}，代码={exc.code}"
            )
        except Exception:
            session.state = "error"
            session.error_code = "unknown"
            session.error = "登录流程发生未预期错误，请刷新重试。"
            logger.exception(f"[agently] Web 登录未预期异常：账号={session.account}")
        else:
            me, _refresh_err = await self._refresh_me_after_write(session.account, client)
            primary = next((a for a in me.aliases if a.is_primary), None) if me is not None else None
            session.email = (primary.email or primary.alias_id) if primary else None
            session.state = "success"
            session.qr_image = None
            session.qr_expired = False
            session.mails = None
            session.error = None
            session.error_code = None
            logger.info(f"[agently] Web 登录完成：账号={session.account}")
        finally:
            for child in (qr_task, oauth_task):
                if child is not None and not child.done():
                    child.cancel()
            pending_children = [c for c in (qr_task, oauth_task) if c is not None]
            if pending_children:
                await asyncio.gather(*pending_children, return_exceptions=True)
            if session.mail_choice is not None and not session.mail_choice.done():
                session.mail_choice.cancel()
            session.func3_result = None
            session.qr_session = None
            if session.headless is not None:
                headless = session.headless
                session.headless = None
                await self._close_headless(session.account, headless)
            if self._active_login_id == session.session_id:
                self._active_login_id = None

    def _download_root(self) -> Path:
        """返回 Web 附件下载暂存目录，交由 AstrBot 临时目录统一管理。"""
        return (
            Path(get_astrbot_temp_path())
            / "astrbot_plugin_qq_agentlymail"
            / "web-downloads"
        )

    def _stage_download(self, content: bytes, filename: str) -> Path:
        """把附件字节写入 AstrBot 临时目录，返回文件路径。"""
        root = self._download_root()
        logger.debug(f"[agently] 下载暂存开始：根目录={root}，字节数={len(content)}")
        subdir = root / uuid.uuid4().hex
        subdir.mkdir(parents=True, exist_ok=True)
        path = subdir / filename
        path.write_bytes(content)
        logger.debug(f"[agently] 下载暂存完成：目录={subdir}，字节数={len(content)}")
        return path

    async def _resolve_account(
        self, account: str
    ) -> tuple[AgentlyMailClient, TokenBundle]:
        """校验账号名并确认凭证存在，返回客户端与凭证。"""
        name = _validate_account_name(account)
        bundle = await self._plugin._token_store.load(name)
        if bundle is None:
            raise WebApiError("账号不存在或未登录，请在聊天中执行 /agently login。", 404)
        client, _oauth = self._plugin._clients_for(name)
        return client, bundle

    async def _resolve_alias(self, client: AgentlyMailClient, alias_id: str) -> str:
        """校验 alias 归属；优先用客户端缓存，未命中时刷新一次身份。"""
        _validate_identifier(alias_id, "alias_id")
        me = client._me
        if me is None or not any(a.alias_id == alias_id for a in me.aliases):
            me = await _timed(client.me(), READ_TIMEOUT)
        if not any(a.alias_id == alias_id for a in me.aliases):
            raise WebApiError("邮箱地址不存在或不属于该账号。", 404)
        return alias_id

    async def _record_send_safe(self, account: str) -> bool:
        """远端发送成功后记录本地计数，失败只记日志，不影响发送结果。"""
        try:
            await self._plugin._record_send(account)
        except Exception:
            logger.exception(f"账号 {account} 远端发送成功但本地发送计数失败")
            return False
        logger.debug(f"[agently] Web 发送计数保存完成：账号={account}")
        return True

    async def _refresh_me_after_write(
        self, account: str, client: AgentlyMailClient
    ) -> tuple[Me | None, str | None]:
        """写成功后刷新身份；刷新失败返回安全错误文本，不影响写结果。"""
        try:
            me = await _timed(client.me(), READ_TIMEOUT)
        except OAuthError as exc:
            logger.warning(
                f"[agently] 写成功后身份刷新失败：账号={account}，异常={type(exc).__name__}，"
                f"代码={exc.code}"
            )
            return None, "凭证失效，请在聊天中重新执行 /agently login。"
        except ClientError as exc:
            logger.warning(
                f"[agently] 写成功后身份刷新失败：账号={account}，异常=ClientError，"
                f"代码={exc.code}，状态码={exc.status_code}"
            )
            if exc.status_code == 401:
                return None, "凭证失效，请在聊天中重新执行 /agently login。"
            return None, "刷新邮箱信息失败，请稍后重试。"
        except WebApiError as exc:
            logger.debug(
                f"[agently] 写成功后身份刷新超时降级：账号={account}，状态码={exc.status_code}"
            )
            return None, exc.message
        self._plugin._me_cache[account] = me
        logger.debug(
            f"[agently] 写成功后身份刷新完成：账号={account}，地址数量={len(me.aliases)}"
        )
        return me, None

    async def _account_view(self, account: str) -> dict[str, Any]:
        """构造单个账号视图，任何失败都隔离为该账号的状态。"""
        view: dict[str, Any] = {
            "account": account,
            "status": "error",
            "app_id": None,
            "expires_at": None,
            "email": None,
            "aliases": None,
            "error": None,
        }
        try:
            bundle = await self._plugin._token_store.load(account)
        except TokenStoreError:
            view["status"] = "invalid_credentials"
            view["error"] = "凭证文件无效，请在聊天中重新执行 /agently login。"
            return view
        if bundle is None:
            view["status"] = "invalid_credentials"
            view["error"] = "凭证不存在，请在聊天中执行 /agently login。"
            return view
        view["app_id"] = bundle.app_id
        view["expires_at"] = _iso(bundle.expires_at)
        try:
            client, _oauth = self._plugin._clients_for(account)
            me = await _timed(client.me(), READ_TIMEOUT)
        except (ClientError, OAuthError) as exc:
            if isinstance(exc, OAuthError) or exc.status_code == 401:
                view["status"] = "auth_expired"
                view["error"] = "凭证失效，请在聊天中重新执行 /agently login。"
            else:
                view["status"] = "error"
                view["error"] = "读取邮箱信息失败，请稍后重试。"
            return view
        except WebApiError:
            view["status"] = "error"
            view["error"] = "读取邮箱信息超时，请稍后重试。"
            return view
        except Exception:
            logger.exception(f"账号 {account} 视图构造失败")
            view["status"] = "error"
            view["error"] = "读取邮箱信息失败，详情见插件日志。"
            return view
        self._plugin._me_cache[account] = me
        view["status"] = "ok"
        view["aliases"] = [_alias_dict(a) for a in me.aliases]
        primary = next((a for a in me.aliases if a.is_primary), None)
        view["email"] = (primary.email or primary.alias_id) if primary else None
        return view

    @_guarded
    async def accounts(self) -> Any:
        """列出全部账号与凭证状态，单账号失败不影响其他账号。"""
        names = await self._plugin._token_store.list_accounts()
        views = await asyncio.gather(*(self._account_view(name) for name in names))
        return json_response({"accounts": list(views)})

    @_guarded
    async def me(self) -> Any:
        """读取身份、限额、约束与凭证状态，账号信息失败时降级。"""
        account = _query_str("account")
        client, bundle = await self._resolve_account(account)
        me = await _timed(client.me(), READ_TIMEOUT)
        self._plugin._me_cache[account] = me
        account_info = None
        try:
            info = await _timed(client.get_account(), READ_TIMEOUT)
            account_info = {
                "nickname": info.get("nickname"),
                "agent_alias_limit": info.get("agent_alias_limit"),
            }
        except (ClientError, OAuthError, WebApiError) as exc:
            logger.debug(
                f"[agently] 账号附加信息读取降级：账号={account}，异常={type(exc).__name__}"
            )
            account_info = None
        return json_response(
            {
                "account": account,
                "aliases": [_alias_dict(a) for a in me.aliases],
                "rate_limits": me.rate_limits.values,
                "constraints": me.constraints.values,
                "scopes": me.scopes,
                "credential": {
                    "app_id": bundle.app_id,
                    "expires_at": _iso(bundle.expires_at),
                    "expired": bundle.expires_at <= time.time(),
                },
                "sent_today_local": await self._plugin._send_count_today(account),
                "account_info": account_info,
            }
        )

    @_guarded
    async def messages(self) -> Any:
        """按别名与目录分页读取邮件摘要，不伪造正文与附件。"""
        account = _query_str("account")
        client, _bundle = await self._resolve_account(account)
        alias_id = await self._resolve_alias(client, _query_str("alias_id"))
        direction = _query_str("dir", required=False, default="inbox")
        if direction not in {"inbox", "sent", "trash", "spam"}:
            raise WebApiError("dir 必须是 inbox/sent/trash/spam 之一。")
        limit = _query_int("limit", default=20, lo=1, hi=50)
        cursor = _query_str("cursor", required=False)
        if cursor is not None and any(ord(c) < 32 for c in cursor):
            raise WebApiError("参数 cursor 含非法字符。")
        unread_raw = _query_str("unread", required=False)
        unread = None
        if unread_raw is not None:
            if unread_raw not in {"true", "false"}:
                raise WebApiError("unread 必须是 true 或 false。")
            unread = unread_raw == "true"
        page = await _timed(
            client.list_messages(
                alias_id=alias_id,
                limit=limit,
                direction=direction,
                cursor=cursor,
                unread=unread,
            ),
            READ_TIMEOUT,
        )
        return json_response(_page_dict(page))

    @_guarded
    async def search(self) -> Any:
        """调用服务端真实搜索，不在本地拉全量过滤。"""
        account = _query_str("account")
        client, _bundle = await self._resolve_account(account)
        alias_id = await self._resolve_alias(client, _query_str("alias_id"))
        query = _query_str("q")
        search_in = _query_str("search_in", required=False, default="SEARCH_IN_ALL")
        if search_in not in {"SEARCH_IN_ALL", "SEARCH_IN_SUBJECT", "SEARCH_IN_CONTENT"}:
            raise WebApiError("search_in 不是支持的搜索范围。")
        limit = _query_int("limit", default=20, lo=1, hi=50)
        cursor = _query_str("cursor", required=False)
        if cursor is not None and any(ord(c) < 32 for c in cursor):
            raise WebApiError("参数 cursor 含非法字符。")
        page = await _timed(
            client.search(
                query,
                alias_id=alias_id,
                limit=limit,
                search_in=search_in,
                cursor=cursor,
            ),
            READ_TIMEOUT,
        )
        return json_response(_page_dict(page))

    @_guarded
    async def message(self) -> Any:
        """读取邮件全文，附件只返回元数据。"""
        account = _query_str("account")
        client, _bundle = await self._resolve_account(account)
        alias_id = await self._resolve_alias(client, _query_str("alias_id"))
        message_id = _query_str("message_id")
        _validate_identifier(message_id, "message_id")
        message = await _timed(client.read(message_id, alias_id=alias_id), READ_TIMEOUT)
        return json_response(_message_dict(message))

    @_guarded
    async def attachment(self) -> Any:
        """重读邮件定位附件后下载，只信任上游响应中的 download_url。"""
        account = _query_str("account")
        client, _bundle = await self._resolve_account(account)
        alias_id = await self._resolve_alias(client, _query_str("alias_id"))
        message_id = _query_str("message_id")
        _validate_identifier(message_id, "message_id")
        attachment_id = _query_str("attachment_id", required=False)
        index_raw = _query_str("index", required=False)
        if (attachment_id is None) == (index_raw is None):
            raise WebApiError("attachment_id 与 index 必须二选一。")
        index = -1
        if attachment_id is not None:
            _validate_identifier(attachment_id, "attachment_id")
        else:
            try:
                index = int(index_raw)
            except ValueError as exc:
                raise WebApiError("参数 index 必须是整数。") from exc
            if index < 0:
                raise WebApiError("参数 index 不能为负。")
        message = await _timed(client.read(message_id, alias_id=alias_id), READ_TIMEOUT)
        if attachment_id is not None:
            target = next(
                (a for a in message.attachments if a.attachment_id == attachment_id), None
            )
        else:
            target = message.attachments[index] if index < len(message.attachments) else None
        if target is None:
            raise WebApiError("附件不存在。", 404)
        if target.download_url:
            content = await _timed(
                client.download(message_id, download_url=target.download_url),
                DOWNLOAD_TIMEOUT,
            )
        else:
            if not target.attachment_id:
                raise WebApiError("附件缺少可下载标识。", 502)
            content = await _timed(
                client.download(message_id, target.attachment_id, alias_id=alias_id),
                DOWNLOAD_TIMEOUT,
            )
        filename = _safe_filename(target.filename)
        content_type = target.content_type or "application/octet-stream"
        path = await asyncio.to_thread(self._stage_download, content, filename)
        return file_response(path, filename=filename, content_type=content_type)

    @_guarded
    async def trash(self) -> Any:
        """把邮件移入垃圾箱（软删除）。"""
        payload = await _read_json_body()
        account = _body_str(payload, "account")
        client, _bundle = await self._resolve_account(account)
        alias_id = await self._resolve_alias(client, _body_str(payload, "alias_id"))
        message_id = _body_str(payload, "message_id")
        _validate_identifier(message_id, "message_id")
        await _timed(client.trash(message_id, alias_id=alias_id), WRITE_TIMEOUT, write=True)
        logger.info(
            f"[agently] Web 邮件已移入垃圾箱：账号={account}，地址ID={alias_id}，"
            f"邮件ID={message_id}"
        )
        return json_response({"message_id": message_id, "trashed": True})

    @_guarded
    async def send(self) -> Any:
        """发送邮件，复用 client 两阶段确认（WebUI 面向管理员，不受聊天发件白名单限制）。"""
        payload = await _read_json_body()
        account = _body_str(payload, "account")
        client, _bundle = await self._resolve_account(account)
        alias_id = await self._resolve_alias(client, _body_str(payload, "alias_id"))
        to = _recipients(payload, "to", required=True)
        cc = _recipients(payload, "cc", required=False)
        bcc = _recipients(payload, "bcc", required=False)
        subject = _body_str(payload, "subject")
        _check_utf8_len(subject, MAX_SUBJECT_BYTES, "subject")
        body = _body_text(payload, "body")
        _check_utf8_len(body, MAX_BODY_TEXT_BYTES, "body")
        body_format = _body_str(payload, "body_format")
        if body_format not in {"HTML", "PLAIN", "MARKDOWN"}:
            raise WebApiError("body_format 必须是 HTML/PLAIN/MARKDOWN 之一。")
        attachments = _build_attachments(payload.get("attachments"))
        await _timed(
            client.send(
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                body=body,
                body_format=body_format,
                alias_id=alias_id,
                attachments=attachments,
            ),
            WRITE_TIMEOUT,
            write=True,
        )
        local_count_recorded = await self._record_send_safe(account)
        logger.info(
            f"[agently] Web 邮件发送成功：账号={account}，地址ID={alias_id}，"
            f"收件人数量={len(to)}，抄送数量={len(cc)}，密送数量={len(bcc)}，"
            f"附件数量={len(attachments)}，本地计数已保存={local_count_recorded}"
        )
        return json_response(
            {
                "queued": True,
                "to": to,
                "subject": subject,
                "attachment_count": len(attachments),
                "local_count_recorded": local_count_recorded,
            }
        )

    @_guarded
    async def reply(self) -> Any:
        """回复邮件（WebUI 面向管理员，不受聊天发件白名单限制）。"""
        payload = await _read_json_body()
        account = _body_str(payload, "account")
        client, _bundle = await self._resolve_account(account)
        alias_id = await self._resolve_alias(client, _body_str(payload, "alias_id"))
        message_id = _body_str(payload, "message_id")
        _validate_identifier(message_id, "message_id")
        body = _body_text(payload, "body")
        _check_utf8_len(body, MAX_BODY_TEXT_BYTES, "body")
        body_format = _body_str(payload, "body_format")
        if body_format not in {"HTML", "PLAIN", "MARKDOWN"}:
            raise WebApiError("body_format 必须是 HTML/PLAIN/MARKDOWN 之一。")
        reply_all = _body_bool(payload, "reply_all", False)
        await _timed(
            client.reply(
                message_id,
                body=body,
                body_format=body_format,
                reply_all=reply_all,
                alias_id=alias_id,
            ),
            WRITE_TIMEOUT,
            write=True,
        )
        local_count_recorded = await self._record_send_safe(account)
        logger.info(
            f"[agently] Web 邮件回复成功：账号={account}，地址ID={alias_id}，"
            f"邮件ID={message_id}，回复全部={reply_all}，"
            f"本地计数已保存={local_count_recorded}"
        )
        return json_response(
            {
                "queued": True,
                "message_id": message_id,
                "reply_all": reply_all,
                "local_count_recorded": local_count_recorded,
            }
        )

    @_guarded
    async def forward(self) -> Any:
        """转发邮件，原附件由服务端按 include_attachments 携带。"""
        payload = await _read_json_body()
        account = _body_str(payload, "account")
        client, _bundle = await self._resolve_account(account)
        alias_id = await self._resolve_alias(client, _body_str(payload, "alias_id"))
        message_id = _body_str(payload, "message_id")
        _validate_identifier(message_id, "message_id")
        to = _recipients(payload, "to", required=True)
        body = _body_text(payload, "body")
        _check_utf8_len(body, MAX_BODY_TEXT_BYTES, "body")
        body_format = _body_str(payload, "body_format")
        if body_format not in {"HTML", "PLAIN", "MARKDOWN"}:
            raise WebApiError("body_format 必须是 HTML/PLAIN/MARKDOWN 之一。")
        include_attachments = _body_bool(payload, "include_attachments", True)
        await _timed(
            client.forward(
                message_id,
                to=to,
                body=body,
                body_format=body_format,
                include_attachments=include_attachments,
                alias_id=alias_id,
            ),
            WRITE_TIMEOUT,
            write=True,
        )
        local_count_recorded = await self._record_send_safe(account)
        logger.info(
            f"[agently] Web 邮件转发成功：账号={account}，地址ID={alias_id}，"
            f"邮件ID={message_id}，收件人数量={len(to)}，包含附件={include_attachments}，"
            f"本地计数已保存={local_count_recorded}"
        )
        return json_response(
            {
                "queued": True,
                "message_id": message_id,
                "to": to,
                "include_attachments": include_attachments,
                "local_count_recorded": local_count_recorded,
            }
        )

    @_guarded
    async def create_address(self) -> Any:
        """新建邮箱地址（支持可选自定义前缀），写成功后刷新身份失败时仍返回写结果。"""
        payload = await _read_json_body()
        account = _body_str(payload, "account")
        raw_prefix = payload.get("prefix")
        prefix: str | None = None
        if raw_prefix is not None:
            if not isinstance(raw_prefix, str):
                raise WebApiError("字段 prefix 必须是字符串。")
            stripped = raw_prefix.strip()
            if stripped:
                prefix = stripped
        client, _bundle = await self._resolve_account(account)
        call = (
            client.create_address(prefix=prefix)
            if prefix is not None
            else client.create_address()
        )
        result = await _timed(call, WRITE_TIMEOUT, write=True)
        alias_id = result.get("alias_id")
        me, refresh_error = await self._refresh_me_after_write(account, client)
        logger.info(
            f"[agently] Web 邮箱地址创建成功：账号={account}，地址ID={alias_id}，"
            f"自定义前缀={prefix is not None}，"
            f"是否复用={bool(result.get('is_reused', False))}，刷新降级={me is None}"
        )
        aliases = [_alias_dict(a) for a in me.aliases] if me is not None else None
        alias = None
        if me is not None:
            alias = next((a for a in me.aliases if a.alias_id == alias_id), None)
        if alias is not None:
            alias_view = _alias_dict(alias)
        else:
            alias_view = {
                "alias_id": alias_id,
                "email": result.get("alias"),
                "is_primary": False,
            }
        return json_response(
            {
                "alias": alias_view,
                "reused": bool(result.get("is_reused", False)),
                "aliases": aliases,
                "refresh_required": me is None,
                "refresh_error": refresh_error,
            }
        )

    @_guarded
    async def modify_address(self) -> Any:
        """修改已有邮箱地址前缀，刷新失败时降级返回写结果。"""
        payload = await _read_json_body()
        account = _body_str(payload, "account")
        client, _bundle = await self._resolve_account(account)
        alias_id = await self._resolve_alias(client, _body_str(payload, "alias_id"))
        prefix = _body_str(payload, "prefix")
        result = await _timed(
            client.modify_address(alias_id, prefix), WRITE_TIMEOUT, write=True
        )
        me, refresh_error = await self._refresh_me_after_write(account, client)
        logger.info(
            f"[agently] Web 邮箱地址修改成功：账号={account}，地址ID={alias_id}，"
            f"刷新降级={me is None}"
        )
        aliases = [_alias_dict(a) for a in me.aliases] if me is not None else None
        alias = None
        if me is not None:
            alias = next((a for a in me.aliases if a.alias_id == alias_id), None)
        if alias is not None:
            alias_view = _alias_dict(alias)
        else:
            alias_view = {
                "alias_id": alias_id,
                "email": result.get("alias"),
                "is_primary": False,
            }
        return json_response(
            {
                "alias": alias_view,
                "aliases": aliases,
                "refresh_required": me is None,
                "refresh_error": refresh_error,
            }
        )

    @_guarded
    async def delete_address(self) -> Any:
        """注销邮箱地址并失效默认别名缓存，刷新失败时降级返回写结果。"""
        payload = await _read_json_body()
        account = _body_str(payload, "account")
        client, _bundle = await self._resolve_account(account)
        alias_id = await self._resolve_alias(client, _body_str(payload, "alias_id"))
        await _timed(client.delete_address(alias_id), WRITE_TIMEOUT, write=True)
        if client._alias_id == alias_id:
            client._alias_id = None
        me, refresh_error = await self._refresh_me_after_write(account, client)
        logger.info(
            f"[agently] Web 邮箱地址注销成功：账号={account}，地址ID={alias_id}，"
            f"刷新降级={me is None}"
        )
        aliases = [_alias_dict(a) for a in me.aliases] if me is not None else None
        return json_response(
            {
                "alias_id": alias_id,
                "aliases": aliases,
                "refresh_required": me is None,
                "refresh_error": refresh_error,
            }
        )

    @_guarded
    async def login_start(self) -> Any:
        """发起或刷新 WebUI 微信扫码登录，返回二维码图片、原链接和验证码。"""
        payload = await _read_json_body()
        raw_account = payload.get("account")
        if raw_account is None:
            account = "default"
        elif isinstance(raw_account, str):
            account = _validate_account_name(raw_account)
        else:
            raise WebApiError("字段 account 必须是字符串。")

        async with self._login_lock:
            if self._active_login_id is not None:
                previous = self._login_sessions.get(self._active_login_id)
                if previous is not None:
                    logger.debug(
                        f"[agently] Web 登录取消旧会话：原账号={previous.account}，"
                        f"原会话={previous.session_id}"
                    )
                    await self._cancel_session(previous)

            client, oauth = self._plugin._clients_for(account)
            try:
                flow = await _timed(oauth.start(), READ_TIMEOUT)
            except OAuthError as exc:
                logger.warning(
                    f"[agently] Web 登录设备流发起失败：账号={account}，代码={exc.code}"
                )
                raise WebApiError("发起登录失败，请稍后重试。", 502) from exc
            deadline = time.monotonic() + flow.expires_in
            expires_at = time.time() + flow.expires_in

            headless: HeadlessQRLogin | None = None
            qr_session: HeadlessQRSession | None = None
            user_code: str | None = None
            try:
                user_code = HeadlessQRLogin.user_code_from_browser_url(flow.browser_url)
                headless = self._make_headless()
                qr_session = await _timed(
                    headless.create_qr_session(deadline=deadline),
                    READ_TIMEOUT,
                )
            except (HeadlessQRLoginError, WebApiError) as exc:
                logger.warning(
                    f"[agently] Web 无头二维码创建失败，降级复制链接模式：账号={account}，"
                    f"代码={getattr(exc, 'code', None)}"
                )
                if headless is not None:
                    await self._close_headless(account, headless)
                headless = None
                qr_session = None
            except Exception:
                logger.exception(
                    f"[agently] Web 无头二维码创建异常，降级复制链接模式：账号={account}"
                )
                if headless is not None:
                    await self._close_headless(account, headless)
                headless = None
                qr_session = None

            mode = "qr" if (headless is not None and qr_session is not None and user_code) else "browser"
            session = _WebLoginSession(
                session_id=uuid.uuid4().hex,
                account=account,
                mode=mode,
                flow=flow,
                deadline=deadline,
                expires_at=expires_at,
                qr_image=_qr_data_url(qr_session.image) if qr_session is not None else None,
                headless=headless,
                qr_session=qr_session,
                user_code=user_code,
            )
            self._login_sessions[session.session_id] = session
            self._active_login_id = session.session_id
            self._prune_login_sessions()

            task = asyncio.create_task(self._run_login_session(session, client, oauth))
            session.task = task
            plugin_tasks = getattr(self._plugin, "_tasks", None)
            if isinstance(plugin_tasks, set):
                plugin_tasks.add(task)
                task.add_done_callback(plugin_tasks.discard)
            # 让后台任务立即进入 try/finally 边界，避免返回后立刻卸载时跳过无头客户端清理。
            await asyncio.sleep(0)

            logger.info(
                f"[agently] Web 登录已发起：账号={account}，会话={session.session_id}，"
                f"模式={'二维码' if mode == 'qr' else '原链接'}"
            )
            return json_response(session.to_dict())

    @_guarded
    async def login_status(self) -> Any:
        """轮询 WebUI 登录会话的当前状态快照。"""
        session_id = _query_str("session_id")
        _validate_identifier(session_id, "session_id")
        session = self._login_sessions.get(session_id)
        if session is None:
            raise WebApiError("登录会话不存在或已失效。", 404)
        return json_response(session.to_dict())

    @_guarded
    async def login_select(self) -> Any:
        """多邮箱账号在扫码确认后选择目标邮箱。"""
        payload = await _read_json_body()
        session_id = _body_str(payload, "session_id")
        _validate_identifier(session_id, "session_id")
        email = _body_str(payload, "email")
        session = self._login_sessions.get(session_id)
        if session is None:
            raise WebApiError("登录会话不存在或已失效。", 404)
        if (
            session.state != "select_mail"
            or session.mail_choice is None
            or session.mail_choice.done()
            or not session.mails
        ):
            raise WebApiError("当前登录会话不处于邮箱选择阶段。", 409)
        allowed = {str(item.get("email", "")) for item in session.mails}
        if email not in allowed:
            raise WebApiError("所选邮箱不在可用列表中。", 400)
        session.state = "authorizing"
        session.mail_choice.set_result(email)
        logger.debug(
            f"[agently] Web 登录已选择邮箱：账号={session.account}，会话={session.session_id}"
        )
        return json_response(session.to_dict())

    @_guarded
    async def login_cancel(self) -> Any:
        """中断当前或指定的 WebUI 登录会话。"""
        payload = await _read_json_body()
        raw_id = payload.get("session_id")
        if raw_id is not None:
            if not isinstance(raw_id, str) or not raw_id.strip():
                raise WebApiError("字段 session_id 必须是非空字符串。")
            session_id = raw_id.strip()
            _validate_identifier(session_id, "session_id")
        else:
            session_id = self._active_login_id

        async with self._login_lock:
            session = self._login_sessions.get(session_id) if session_id else None
            if session is None:
                return json_response({"cancelled": False, "session": None})
            await self._cancel_session(session)
            logger.info(
                f"[agently] Web 登录已中断：账号={session.account}，会话={session.session_id}"
            )
            return json_response({"cancelled": True, "session": session.to_dict()})


def register_web_apis(plugin: QQAgentlyMailPlugin) -> MailWebApi:
    """创建 Web API 实例并向插件 context 注册全部路由。"""
    api = MailWebApi(plugin)
    routes: list[tuple[str, str, Callable[..., Awaitable[Any]], str]] = [
        ("GET", "accounts", api.accounts, "列出账号与凭证状态"),
        ("GET", "me", api.me, "读取身份、限额与凭证状态"),
        ("GET", "messages", api.messages, "分页读取邮件摘要"),
        ("GET", "search", api.search, "服务端搜索邮件"),
        ("GET", "message", api.message, "读取邮件全文"),
        ("GET", "attachment", api.attachment, "下载附件"),
        ("POST", "trash", api.trash, "把邮件移入垃圾箱"),
        ("POST", "send", api.send, "发送邮件"),
        ("POST", "reply", api.reply, "回复邮件"),
        ("POST", "forward", api.forward, "转发邮件"),
        ("POST", "create_address", api.create_address, "新建邮箱地址"),
        ("POST", "modify_address", api.modify_address, "修改邮箱地址前缀"),
        ("POST", "delete_address", api.delete_address, "注销邮箱地址"),
        ("POST", "login_start", api.login_start, "发起或刷新 WebUI 微信扫码登录"),
        ("GET", "login_status", api.login_status, "查询 WebUI 扫码登录状态"),
        ("POST", "login_select", api.login_select, "选择多邮箱账号的目标邮箱"),
        ("POST", "login_cancel", api.login_cancel, "中断 WebUI 扫码登录"),
    ]
    for method, name, handler, desc in routes:
        plugin.context.register_web_api(f"{ROUTE_PREFIX}/{name}", handler, [method], desc)
    logger.info(f"[agently] Web API 路由注册完成：数量={len(routes)}")
    return api
