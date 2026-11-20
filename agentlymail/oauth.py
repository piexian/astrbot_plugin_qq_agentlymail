"""实现 QQ Agently Mail OAuth 设备流和凭证刷新。"""

from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from typing import Any, Mapping

import httpx

from astrbot.api import logger

from .constants import AUTH_BASE, CLI_VERSION, DEVICE_PATH, TOKEN_PATH, USER_AGENT
from .token_store import TokenBundle, TokenStore


def _safe_endpoint(value: str) -> str:
    """返回不含 query 和用户信息的安全请求目标。"""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid-url>"
    return f"{parsed.hostname or '-'}{parsed.path or '/'}"


def _trusted_agent_url(value: str) -> bool:
    """只接受 agent.qq.com 信任域的 HTTPS 设备流地址。"""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    host = parsed.hostname
    return (
        parsed.scheme == "https"
        and host is not None
        and (host == "agent.qq.com" or host.endswith(".agent.qq.com"))
        and port is None
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )

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
        if not _trusted_agent_url(poll_url):
            raise OAuthError(
                "invalid_response",
                "设备流响应的 poll_url 不是可信 HTTPS 地址",
            )
        if not _trusted_agent_url(browser_url):
            raise OAuthError(
                "invalid_response",
                "设备流响应的 browser_url 不是可信 HTTPS 地址",
            )
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
        logger.debug(f"[agently] OAuth 设备流开始 账号={self.account}")
        try:
            existing = await self.token_store.load(self.account)
            logger.debug(
                f"[agently] OAuth 设备流凭证检查 账号={self.account} "
                f"复用应用标识={bool(existing is not None and existing.app_id)}"
            )
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
            flow = DeviceFlowStart.from_payload(await self._json(response, "设备流初始化"))
            logger.debug(
                f"[agently] OAuth 设备流初始化完成 账号={self.account} "
                f"有效期={flow.expires_in:.0f}"
            )
            return flow
        except Exception as exc:
            logger.debug(
                f"[agently] OAuth 设备流初始化失败 账号={self.account} "
                f"异常={type(exc).__name__} 代码={getattr(exc, 'code', None)}"
            )
            raise

    async def poll(
        self,
        flow: DeviceFlowStart,
        deadline: float | None = None,
    ) -> TokenBundle:
        """轮询设备授权直到完成或超时并保存新凭证。"""
        flow_deadline = time.monotonic() + flow.expires_in
        if deadline is None or deadline > flow_deadline:
            deadline = flow_deadline
        interval = self.poll_interval
        attempt = 0
        logger.debug(
            f"[agently] OAuth 设备流轮询开始 账号={self.account} "
            f"轮询间隔={interval:.1f} 剩余秒数={max(0.0, deadline - time.monotonic()):.1f}"
        )
        try:
            while True:
                if time.monotonic() >= deadline:
                    raise AuthorizationPendingError("设备流在 expires_in 内未完成授权")
                attempt += 1
                response = await self._request("GET", flow.poll_url)
                try:
                    payload = await self._json(response, "设备流轮询")
                except AuthorizationPendingError:
                    logger.debug(
                        f"[agently] OAuth 授权等待中 账号={self.account} "
                        f"轮询次数={attempt} 轮询间隔={interval:.1f}"
                    )
                    await self._sleep_until_next(interval, deadline)
                    continue
                except SlowDownError:
                    interval += 5.0
                    logger.debug(
                        f"[agently] OAuth 收到 slow_down 账号={self.account} "
                        f"轮询次数={attempt} 下次轮询间隔={interval:.1f}"
                    )
                    await self._sleep_until_next(interval, deadline)
                    continue
                error_code = self._error_code(payload)
                if error_code == "authorized" or payload.get("status") == "authorized":
                    bundle = self._bundle_from_authorized(payload)
                    logger.debug(f"[agently] OAuth 授权成功，准备落盘 账号={self.account}")
                    await self.token_store.save(self.account, bundle)
                    logger.info(f"[agently] OAuth 设备授权完成 账号={self.account}")
                    return bundle
                if error_code in {"authorization_pending", "pending"} or payload.get("status") in {
                    "authorization_pending",
                    "pending",
                }:
                    logger.debug(
                        f"[agently] OAuth 授权等待中 账号={self.account} "
                        f"轮询次数={attempt} 轮询间隔={interval:.1f}"
                    )
                    await self._sleep_until_next(interval, deadline)
                    continue
                if error_code == "slow_down":
                    interval += 5.0
                    logger.debug(
                        f"[agently] OAuth 收到 slow_down 账号={self.account} "
                        f"轮询次数={attempt} 下次轮询间隔={interval:.1f}"
                    )
                    await self._sleep_until_next(interval, deadline)
                    continue
                if error_code:
                    raise self._mapped_error(error_code, payload)
                raise OAuthError("invalid_response", "设备流轮询返回未知状态")
        except Exception as exc:
            logger.debug(
                f"[agently] OAuth 设备流轮询失败 账号={self.account} "
                f"轮询次数={attempt} 异常={type(exc).__name__} "
                f"代码={getattr(exc, 'code', None)}"
            )
            raise

    async def refresh(self) -> TokenBundle:
        """在账号锁内刷新并立即轮换落盘 refresh token。"""
        logger.debug(f"[agently] OAuth 刷新开始，等待账号锁 账号={self.account}")
        try:
            async with self.token_store.locked(self.account):
                logger.debug(f"[agently] OAuth 刷新已获取账号锁 账号={self.account}")
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
                logger.debug(f"[agently] OAuth 刷新响应有效，准备轮换落盘 账号={self.account}")
                await self.token_store._save_unlocked(self.account, bundle)
                logger.info(f"[agently] OAuth 凭证刷新并落盘完成 账号={self.account}")
                return bundle
        except Exception as exc:
            logger.debug(
                f"[agently] OAuth 刷新失败 账号={self.account} "
                f"异常={type(exc).__name__} 代码={getattr(exc, 'code', None)}"
            )
            raise

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        endpoint = _safe_endpoint(url)
        started = time.perf_counter()
        logger.debug(f"[agently] OAuth HTTP 开始 方法={method} 地址={endpoint}")
        headers = dict(kwargs.pop("headers", {}))
        headers["User-Agent"] = USER_AGENT
        try:
            response = await self._http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning(
                f"[agently] OAuth HTTP 传输失败 方法={method} 地址={endpoint} "
                f"耗时={time.perf_counter() - started:.3f}s 异常={type(exc).__name__}"
            )
            raise OAuthError("transport_error", f"请求 {method} {endpoint} 失败") from exc
        logger.debug(
            f"[agently] OAuth HTTP 完成 方法={method} 地址={endpoint} "
            f"状态码={response.status_code} 耗时={time.perf_counter() - started:.3f}s"
        )
        return response

    async def _json(self, response: httpx.Response, operation: str) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            logger.debug(
                f"[agently] OAuth 响应非 JSON 操作={operation} "
                f"状态码={response.status_code}"
            )
            raise OAuthError(
                "invalid_response",
                f"{operation}返回非 JSON，HTTP {response.status_code}",
            ) from exc
        if not isinstance(payload, Mapping):
            logger.debug(
                f"[agently] OAuth 响应不是对象 操作={operation} "
                f"状态码={response.status_code}"
            )
            raise OAuthError("invalid_response", f"{operation}返回的 JSON 不是对象")
        if response.is_error:
            code = self._error_code(payload) or f"http_{response.status_code}"
            logger.debug(
                f"[agently] OAuth 响应业务失败 操作={operation} "
                f"状态码={response.status_code} 代码={code}"
            )
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
