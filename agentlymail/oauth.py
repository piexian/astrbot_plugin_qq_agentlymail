"""实现 QQ Agently Mail OAuth 设备流和凭证刷新。"""

from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from .constants import AUTH_BASE, CLI_VERSION, DEVICE_PATH, TOKEN_PATH, USER_AGENT
from .token_store import TokenBundle, TokenStore


class OAuthError(RuntimeError):
    """表示 OAuth 请求或授权流程失败。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"OAuth {code}: {message}")
        self.code = code
        self.message = message


class AccessDeniedError(OAuthError):
    """表示用户拒绝了设备授权。"""

    def __init__(self, message: str = "用户拒绝设备授权"):
        super().__init__("access_denied", message)


class ExpiredTokenError(OAuthError):
    """表示设备码或 refresh token 已过期。"""

    def __init__(self, message: str = "授权已过期"):
        super().__init__("expired_token", message)


class AuthorizationPendingError(OAuthError):
    """表示设备授权仍在等待用户确认。"""

    def __init__(self, message: str = "等待用户完成设备授权"):
        super().__init__("authorization_pending", message)


class SlowDownError(OAuthError):
    """表示设备流轮询过快。"""

    def __init__(self, message: str = "设备流轮询过快"):
        super().__init__("slow_down", message)


@dataclass(slots=True)
class DeviceFlowStart:
    """保存设备流初始化响应。"""

    poll_url: str
    browser_url: str
    input_code: str
    expires_in: float

    @classmethod
    def from_payload(cls, value: Any) -> DeviceFlowStart:
        if not isinstance(value, Mapping):
            raise OAuthError("invalid_response", "设备流初始化响应不是 JSON 对象")
        poll_url = value.get("poll_url")
        browser_url = value.get("browser_url")
        input_code = value.get("input_code")
        expires_in = value.get("expires_in")
        if not isinstance(poll_url, str) or not poll_url:
            raise OAuthError("invalid_response", "设备流响应缺少 poll_url")
        if not isinstance(browser_url, str) or not browser_url:
            raise OAuthError("invalid_response", "设备流响应缺少 browser_url")
        if not isinstance(input_code, str) or not input_code:
            raise OAuthError("invalid_response", "设备流响应缺少 input_code")
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
            raise OAuthError("invalid_response", "设备流响应缺少数字 expires_in")
        return cls(
            poll_url=poll_url,
            browser_url=browser_url,
            input_code=input_code,
            expires_in=float(expires_in),
        )


_ERROR_TYPES: dict[str, type[OAuthError]] = {
    "access_denied": AccessDeniedError,
    "expired_token": ExpiredTokenError,
    "authorization_pending": AuthorizationPendingError,
    "slow_down": SlowDownError,
}


class OAuthClient:
    """通过设备流取得并刷新一个账号的 OAuth 凭证。"""

    def __init__(
        self,
        token_store: TokenStore,
        account: str,
        http_client: httpx.AsyncClient | None = None,
        poll_interval: float = 5.0,
    ):
        self.token_store = token_store
        self.account = account
        self._http = http_client or httpx.AsyncClient(base_url=AUTH_BASE)
        self._owns_http = http_client is None
        self.poll_interval = max(0.0, poll_interval)

    async def close(self) -> None:
        """关闭由 OAuth 客户端自行创建的 HTTP 客户端。"""
        if self._owns_http:
            await self._http.aclose()

    async def start(self) -> DeviceFlowStart:
        """启动 OAuth 设备流并返回扫码地址和轮询地址。"""
        existing = await self.token_store.load(self.account)
        payload: dict[str, str] = {
            "cli_agentname": "AstrBot",
            "cli_agentua": "astrbot",
            "cli_hostname": socket.gethostname(),
            "cli_ua": f"agently-cli/{CLI_VERSION}",
            "cli_version": CLI_VERSION,
        }
        if existing is not None and existing.app_id:
            payload["app_id"] = existing.app_id
        response = await self._request(
            "POST",
            DEVICE_PATH,
            json=payload,
        )
        return DeviceFlowStart.from_payload(await self._json(response, "设备流初始化"))

    async def poll(self, flow: DeviceFlowStart) -> TokenBundle:
        """轮询设备授权直到完成或超时并保存新凭证。"""
        deadline = time.monotonic() + flow.expires_in
        interval = self.poll_interval
        while True:
            if time.monotonic() >= deadline:
                raise AuthorizationPendingError("设备流在 expires_in 内未完成授权")
            response = await self._request("GET", flow.poll_url)
            try:
                payload = await self._json(response, "设备流轮询")
            except AuthorizationPendingError:
                await self._sleep_until_next(interval, deadline)
                continue
            except SlowDownError:
                interval += 5.0
                await self._sleep_until_next(interval, deadline)
                continue
            error_code = self._error_code(payload)
            if error_code == "authorized" or payload.get("status") == "authorized":
                bundle = self._bundle_from_authorized(payload)
                await self.token_store.save(self.account, bundle)
                return bundle
            if error_code in {"authorization_pending", "pending"} or payload.get("status") in {
                "authorization_pending",
                "pending",
            }:
                await self._sleep_until_next(interval, deadline)
                continue
            if error_code == "slow_down":
                interval += 5.0
                await self._sleep_until_next(interval, deadline)
                continue
            if error_code:
                raise self._mapped_error(error_code, payload)
            status = payload.get("status")
            raise OAuthError(
                "invalid_response",
                f"设备流轮询返回未知状态: {status!r}",
            )

    async def refresh(self) -> TokenBundle:
        """在账号锁内刷新并立即轮换落盘 refresh token。"""
        async with self.token_store.locked(self.account):
            current = await self.token_store._load_unlocked(self.account)
            if current is None:
                raise OAuthError("missing_credentials", f"账号 {self.account!r} 没有凭证")
            if not current.app_id:
                raise OAuthError("missing_app_id", f"账号 {self.account!r} 凭证缺少 app_id")
            if not current.refresh_token:
                raise OAuthError(
                    "missing_refresh_token",
                    f"账号 {self.account!r} 凭证缺少 refresh_token",
                )
            response = await self._request(
                "POST",
                TOKEN_PATH,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": current.refresh_token,
                    "client_id": current.app_id,
                    "clientversion": CLI_VERSION,
                },
            )
            payload = await self._json(response, "凭证刷新")
            bundle = self._bundle_from_refresh(payload, current.app_id)
            await self.token_store._save_unlocked(self.account, bundle)
            return bundle

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers["User-Agent"] = USER_AGENT
        try:
            return await self._http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise OAuthError("transport_error", f"请求 {method} {url} 失败: {exc}") from exc

    async def _json(self, response: httpx.Response, operation: str) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise OAuthError(
                "invalid_response",
                f"{operation}返回非 JSON，HTTP {response.status_code}",
            ) from exc
        if not isinstance(payload, Mapping):
            raise OAuthError("invalid_response", f"{operation}返回的 JSON 不是对象")
        if response.is_error:
            code = self._error_code(payload) or f"http_{response.status_code}"
            raise self._mapped_error(code, payload, response.status_code)
        return payload

    @staticmethod
    def _error_code(payload: Mapping[str, Any]) -> str | None:
        error = payload.get("error")
        if isinstance(error, str):
            return error
        if isinstance(error, Mapping):
            code = error.get("code")
            if isinstance(code, str):
                return code
        code = payload.get("code")
        return code if isinstance(code, str) else None

    @staticmethod
    def _mapped_error(
        code: str,
        payload: Mapping[str, Any],
        status_code: int | None = None,
    ) -> OAuthError:
        error = payload.get("error")
        if isinstance(error, Mapping):
            message = error.get("message")
        else:
            message = payload.get("message")
        if not isinstance(message, str) or not message:
            message = f"HTTP {status_code}" if status_code is not None else "服务器返回 OAuth 错误"
        error_type = _ERROR_TYPES.get(code)
        if error_type is None:
            return OAuthError(code, message)
        return error_type(message)

    @staticmethod
    def _bundle_from_authorized(payload: Mapping[str, Any]) -> TokenBundle:
        app_id = payload.get("app_id")
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        if not isinstance(app_id, str) or not app_id:
            raise OAuthError("invalid_response", "授权响应缺少 app_id")
        if not isinstance(access_token, str) or not access_token:
            raise OAuthError("invalid_response", "授权响应缺少 access_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise OAuthError("invalid_response", "授权响应缺少 refresh_token")
        return TokenBundle(
            app_id=app_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=OAuthClient._expires_at(expires_in, "授权响应"),
        )

    @staticmethod
    def _bundle_from_refresh(
        payload: Mapping[str, Any],
        app_id: str,
    ) -> TokenBundle:
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        if not isinstance(access_token, str) or not access_token:
            raise OAuthError("invalid_response", "刷新响应缺少 access_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise OAuthError("invalid_response", "刷新响应缺少 refresh_token")
        return TokenBundle(
            app_id=app_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=OAuthClient._expires_at(expires_in, "刷新响应"),
        )

    @staticmethod
    def _expires_at(expires_in: Any, operation: str) -> float:
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
            raise OAuthError("invalid_response", f"{operation}缺少数字 expires_in")
        return time.time() + float(expires_in)

    @staticmethod
    async def _sleep_until_next(interval: float, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AuthorizationPendingError("设备流在 expires_in 内未完成授权")
        await asyncio.sleep(min(interval, remaining))
