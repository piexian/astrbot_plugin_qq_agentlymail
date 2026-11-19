"""按会话键登记在途的设备流登录，防止同一会话重复发起。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class LoginRegistry:
    """按会话键防止并发的设备流登录，条目带过期时间。"""

    def __init__(self, clock: Callable[[], float] | None = None):
        self._clock = clock or time.monotonic
        self._lock = asyncio.Lock()
        self._entries: dict[str, float] = {}

    async def start(self, key: str, ttl: float) -> bool:
        """登记一次登录并返回是否成功，该键已在途且未过期时返回 False。"""
        async with self._lock:
            now = self._clock()
            # 顺手清掉其它键的过期条目，避免字典随会话数无限增长。
            self._entries = {
                entry_key: expires
                for entry_key, expires in self._entries.items()
                if expires > now
            }
            if key in self._entries:
                return False
            self._entries[key] = now + ttl
            return True

    async def finish(self, key: str) -> None:
        """结束一次登录并清除该键的在途标记。"""
        async with self._lock:
            self._entries.pop(key, None)

    async def in_flight(self, key: str) -> bool:
        """查询该键是否有未过期的登录在途。"""
        async with self._lock:
            expires = self._entries.get(key)
            return expires is not None and expires > self._clock()
