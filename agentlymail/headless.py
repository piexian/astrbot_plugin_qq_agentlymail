"""实现无浏览器微信扫码登录所需的 qrconnect 协议链。"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

import httpx


QRCONNECT_URL = "https://open.weixin.qq.com/connect/qrconnect"
QR_CODE_BASE_URL = "https://open.weixin.qq.com"
QR_POLL_URL = "https://lp.open.weixin.qq.com/connect/l/qrconnect"
AGENT_LOGIN_URL = "https://agent.qq.com/agent/login"
WECHAT_APP_ID = "wxb55b95bbb55bac6b"
WECHAT_REDIRECT_URI = "https://agent.qq.com/page/callback"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_QR_CODE_PATH_PATTERN = re.compile(r'src="(/connect/qrcode/[^\"]+)"')
_WX_ERROR_CODE_PATTERN = re.compile(r"window\.wx_errcode\s*=\s*(-?\d+)")
_WX_CODE_PATTERN = re.compile(r"window\.wx_code\s*=\s*'([^']*)'")


@dataclass(frozen=True, slots=True)
class HeadlessQRSession:
    """保存一次 qrconnect 会话及可发送的二维码。"""

    uuid: str
    image: bytes
    expires_hint: str


@dataclass(frozen=True, slots=True)
class AvailableAgentMail:
    """保存 func=3 返回的邮箱地址及服务端选择标记。"""

    email: str
    is_selected: bool


@dataclass(frozen=True, slots=True)
class Func3Result:
    """保存 func=3 返回的邮箱候选和临时票据。"""

    mails: tuple[AvailableAgentMail, ...]
    login_tmpticket: str


class HeadlessQRLoginError(RuntimeError):
    """表示无头二维码登录链路失败。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"无头登录 {code}: {message}")
        self.code = code
        self.message = message


class HeadlessQRLogin:
    """封装 qrconnect、微信轮询和 agent/login func=3/8。"""

    def __init__(self, http_client: httpx.AsyncClient | None = None):
        self._http = http_client or httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        self._owns_http = http_client is None

    async def close(self) -> None:
        """关闭由客户端自行创建的 HTTP 客户端。"""
        if self._owns_http:
            await self._http.aclose()

    @staticmethod
    def user_code_from_browser_url(browser_url: str) -> str:
        """从设备流 browser_url 的 user_code 查询参数中取用户码。"""
        parsed = urlsplit(browser_url)
        values = parse_qs(parsed.query).get("user_code", [])
        if len(values) != 1 or not values[0]:
            raise HeadlessQRLoginError(
                "invalid_browser_url",
                "设备流 browser_url 缺少 user_code",
            )
        return values[0]

    async def create_qr_session(
        self,
        deadline: float | None = None,
    ) -> HeadlessQRSession:
        """创建独立微信扫码会话并下载二维码图片。"""
        try:
            page = await self._request_with_deadline(
                "GET",
                QRCONNECT_URL,
                deadline=deadline,
                params={
                    "appid": WECHAT_APP_ID,
                    "scope": "snsapi_login",
                    "redirect_uri": WECHAT_REDIRECT_URI,
                    "response_type": "code",
                    "state": "state",
                    "lang": "cn",
                    "style": "black",
                    "stylelite": "1",
                    "self_redirect": "true",
                },
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
        except HeadlessQRLoginError:
            raise
        except Exception as exc:
            raise HeadlessQRLoginError("transport_error", f"创建微信扫码会话失败：{exc}") from exc

        match = _QR_CODE_PATH_PATTERN.search(page.text)
        if match is None:
            raise HeadlessQRLoginError(
                "invalid_response",
                "微信扫码会话响应缺少二维码路径",
            )
        qr_path = match.group(1)
        uuid = qr_path.rsplit("/", 1)[-1]
        if not uuid:
            raise HeadlessQRLoginError(
                "invalid_response",
                "微信扫码会话响应缺少二维码 uuid",
            )

        try:
            image = await self._request_with_deadline(
                "GET",
                f"{QR_CODE_BASE_URL}{qr_path}",
                deadline=deadline,
                headers={"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"},
            )
        except HeadlessQRLoginError:
            raise
        except Exception as exc:
            raise HeadlessQRLoginError("transport_error", f"下载微信二维码失败：{exc}") from exc
        if not image.content:
            raise HeadlessQRLoginError("invalid_response", "微信二维码响应为空")

        return HeadlessQRSession(
            uuid=uuid,
            image=image.content,
            expires_hint="二维码约 2-3 分钟有效，过期后请重新执行登录。",
        )

    async def poll_wx_code(
        self,
        session: HeadlessQRSession,
        deadline: float,
        on_status: Callable[[int], Awaitable[None]] | None = None,
    ) -> str:
        """轮询微信状态，收到 405 后返回 wx_code。"""
        last_error_code: int | None = None
        notified_status: int | None = None
        scanned_404_notified = False
        while True:
            params: dict[str, str] = {"uuid": session.uuid}
            if last_error_code is not None:
                params["last"] = str(last_error_code)
            response = await self._request_with_deadline(
                "GET",
                QR_POLL_URL,
                deadline=deadline,
                params=params,
                headers={"Accept": "text/javascript, */*; q=0.01"},
            )
            error_code, wx_code = self._parse_poll_response(response.text)
            if error_code == 404:
                if not scanned_404_notified:
                    scanned_404_notified = True
                    if on_status is not None:
                        await on_status(error_code)
            elif error_code != notified_status:
                if on_status is not None:
                    await on_status(error_code)
            notified_status = error_code
            if error_code == 408:
                continue
            if error_code == 404:
                last_error_code = 404
                continue
            if error_code == 405:
                if not wx_code:
                    raise HeadlessQRLoginError(
                        "invalid_response",
                        "微信确认响应缺少 wx_code",
                    )
                return wx_code
            raise HeadlessQRLoginError(
                str(error_code),
                "微信扫码返回未支持的状态",
            )

    async def func3(
        self,
        user_code: str,
        wx_code: str,
        deadline: float | None = None,
    ) -> Func3Result:
        """用 wx_code 获取可用邮箱和临时登录票据。"""
        payload = await self._post_agent_login(
            {
                "user_code": user_code,
                "code": wx_code,
                "func": "3",
                "language": "zh",
                "r": secrets.token_hex(8),
            },
            deadline=deadline,
            operation="微信登录邮箱查询",
        )
        body = self._body(payload, "微信登录邮箱查询")
        ticket = body.get("login_tmpticket")
        if not isinstance(ticket, str) or not ticket:
            raise HeadlessQRLoginError(
                "invalid_response",
                "微信登录邮箱查询响应缺少 login_tmpticket",
            )
        raw_mails = body.get("avilable_agent_mails")
        if not isinstance(raw_mails, list) or not raw_mails:
            raise HeadlessQRLoginError(
                "invalid_response",
                "微信登录邮箱查询响应缺少可用邮箱",
            )
        mails: list[AvailableAgentMail] = []
        for index, raw_mail in enumerate(raw_mails):
            if not isinstance(raw_mail, Mapping):
                raise HeadlessQRLoginError(
                    "invalid_response",
                    f"可用邮箱 {index} 不是对象",
                )
            email = raw_mail.get("email")
            is_selected = raw_mail.get("is_selected", False)
            if not isinstance(email, str) or not email:
                raise HeadlessQRLoginError(
                    "invalid_response",
                    f"可用邮箱 {index} 缺少 email",
                )
            if not isinstance(is_selected, bool):
                raise HeadlessQRLoginError(
                    "invalid_response",
                    f"可用邮箱 {index} 的 is_selected 不是布尔值",
                )
            mails.append(AvailableAgentMail(email=email, is_selected=is_selected))
        return Func3Result(mails=tuple(mails), login_tmpticket=ticket)

    async def func8(
        self,
        user_code: str,
        email: str,
        login_tmpticket: str,
        deadline: float | None = None,
    ) -> None:
        """显式提交邮箱和临时票据，完成网页登录确认。"""
        await self._post_agent_login(
            {
                "user_code": user_code,
                "func": "8",
                "email": email,
                "login_tmpticket": login_tmpticket,
                "language": "zh",
                "r": secrets.token_hex(8),
            },
            deadline=deadline,
            operation="微信登录邮箱确认",
        )

    async def _post_agent_login(
        self,
        form: Mapping[str, str],
        *,
        deadline: float | None,
        operation: str,
    ) -> Mapping[str, Any]:
        headers = {
            "Origin": "https://agent.qq.com",
            "Referer": "https://agent.qq.com/page/callback",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            response = await self._request_with_deadline(
                "POST",
                AGENT_LOGIN_URL,
                deadline=deadline,
                data=form,
                headers=headers,
                raise_for_status=False,
            )
        except HeadlessQRLoginError:
            raise
        except Exception as exc:
            raise HeadlessQRLoginError("transport_error", f"{operation}失败：{exc}") from exc
        payload = self._json(response, operation)
        ret = payload.get("ret")
        if isinstance(ret, bool) or ret not in (0, "0"):
            code = str(ret) if isinstance(ret, (int, str)) else f"http_{response.status_code}"
            raise HeadlessQRLoginError(code, f"{operation}失败")
        if response.is_error:
            raise HeadlessQRLoginError(f"http_{response.status_code}", f"{operation}失败")
        return payload

    async def _request_with_deadline(
        self,
        method: str,
        url: str,
        *,
        deadline: float | None,
        raise_for_status: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        request_headers = dict(kwargs.pop("headers", {}))
        request_headers.setdefault("User-Agent", BROWSER_USER_AGENT)
        kwargs["headers"] = request_headers
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise HeadlessQRLoginError("expired", "无头二维码登录已超时")
        try:
            request = self._http.request(method, url, **kwargs)
            if remaining is None:
                response = await request
            else:
                response = await asyncio.wait_for(request, timeout=remaining)
            if raise_for_status:
                response.raise_for_status()
            return response
        except asyncio.TimeoutError as exc:
            raise HeadlessQRLoginError("expired", "无头二维码登录已超时") from exc
        except httpx.HTTPError as exc:
            raise HeadlessQRLoginError(
                "transport_error",
                f"请求 {method} {url} 失败：{exc}",
            ) from exc

    @staticmethod
    def _parse_poll_response(text: str) -> tuple[int, str | None]:
        error_match = _WX_ERROR_CODE_PATTERN.search(text)
        if error_match is None:
            raise HeadlessQRLoginError(
                "invalid_response",
                "微信轮询响应缺少 wx_errcode",
            )
        code_match = _WX_CODE_PATTERN.search(text)
        return int(error_match.group(1)), code_match.group(1) if code_match else None

    @staticmethod
    def _json(response: httpx.Response, operation: str) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise HeadlessQRLoginError(
                "invalid_response",
                f"{operation}返回非 JSON，HTTP {response.status_code}",
            ) from exc
        if not isinstance(payload, Mapping):
            raise HeadlessQRLoginError("invalid_response", f"{operation}返回的 JSON 不是对象")
        return payload

    @staticmethod
    def _body(payload: Mapping[str, Any], operation: str) -> Mapping[str, Any]:
        body = payload.get("body")
        if not isinstance(body, Mapping):
            raise HeadlessQRLoginError("invalid_response", f"{operation}响应缺少 body")
        return body
