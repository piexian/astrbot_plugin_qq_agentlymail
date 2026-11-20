"""实现 events/wait 长轮询和邮件事件去重。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from typing import Any

from .client import AgentlyMailClient
from .models import Message


Sleep = Callable[[float], Awaitable[None]]
SaveState = Callable[[str | None, list[str]], Awaitable[None]]
MAX_SEEN_MESSAGE_IDS = 1000


class WatcherError(RuntimeError):
    """表示邮件事件响应缺少协议要求的字段。"""


class MessageWatcher:
    """循环读取 events/wait 并产出事件对应的完整邮件。"""

    def __init__(
        self,
        client: AgentlyMailClient,
        *,
        alias_id: str | None = None,
        next_cursor: str | None = None,
        seen_message_ids: Iterable[str] | None = None,
        save_state: SaveState | None = None,
        empty_delay: float = 1.0,
        max_empty_delay: float = 60.0,
        sleep: Sleep | None = None,
    ):
        if empty_delay < 0 or max_empty_delay < empty_delay:
            raise ValueError("空轮询退避参数必须满足 0 <= empty_delay <= max_empty_delay")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ValueError("初始 next_cursor 必须是字符串或 null")
        self.client = client
        self.alias_id = alias_id
        self.empty_delay = empty_delay
        self.max_empty_delay = max_empty_delay
        self._sleep = sleep or asyncio.sleep
        self._save_state = save_state
        self._next_cursor = next_cursor
        self._seen_message_ids: OrderedDict[str, None] = OrderedDict()
        for message_id in seen_message_ids or ():
            self._remember_seen(message_id)

    @property
    def next_cursor(self) -> str | None:
        """返回当前已确认的事件游标。"""
        return self._next_cursor

    @property
    def seen_message_ids(self) -> list[str]:
        """返回当前已确认的有序去重 ID 快照。"""
        return list(self._seen_message_ids)

    async def watch(self) -> AsyncIterator[Message]:
        """持续等待邮件事件并产出对应的完整邮件。"""
        delay = self.empty_delay
        while True:
            payload = await self.client.events_wait(
                alias_id=self.alias_id,
                next_cursor=self._next_cursor,
            )
            events, response_cursor = self._events_and_cursor(payload)
            if not events:
                if response_cursor is not None:
                    await self._persist_state(response_cursor, self.seen_message_ids)
                await self._sleep(delay)
                delay = min(max(delay * 2, self.empty_delay), self.max_empty_delay)
                continue

            delay = self.empty_delay
            for index, event in enumerate(events):
                message_id = self._message_id(event, index)
                if message_id in self._seen_message_ids:
                    continue
                message = await self.client.read(message_id, alias_id=self.alias_id)
                candidate_seen = self._candidate_seen(message_id)
                yield message
                await self._persist_state(self._next_cursor, candidate_seen)

            if response_cursor is not None:
                await self._persist_state(response_cursor, self.seen_message_ids)

    async def _persist_state(
        self,
        next_cursor: str | None,
        seen_message_ids: Iterable[str],
    ) -> None:
        """先保存候选状态，成功后再提交实例状态。"""
        candidate_seen = list(seen_message_ids)
        if self._save_state is not None:
            await self._save_state(next_cursor, candidate_seen)
        self._next_cursor = next_cursor
        self._seen_message_ids = OrderedDict.fromkeys(candidate_seen)

    def _candidate_seen(self, message_id: str) -> list[str]:
        """生成加入一个新 ID 后的有界去重快照。"""
        candidate = OrderedDict.fromkeys(self._seen_message_ids)
        candidate.pop(message_id, None)
        candidate[message_id] = None
        while len(candidate) > MAX_SEEN_MESSAGE_IDS:
            candidate.popitem(last=False)
        return list(candidate)

    def _remember_seen(self, message_id: str) -> None:
        """把初始去重 ID 加入有界顺序集合。"""
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("seen_message_ids 必须是非空字符串数组")
        self._seen_message_ids.pop(message_id, None)
        self._seen_message_ids[message_id] = None
        while len(self._seen_message_ids) > MAX_SEEN_MESSAGE_IDS:
            self._seen_message_ids.popitem(last=False)

    @staticmethod
    def _events_and_cursor(
        payload: Mapping[str, Any],
    ) -> tuple[list[Any], str | None]:
        container: Mapping[str, Any] = payload
        data = payload.get("data")
        if isinstance(data, Mapping):
            container = data
            events = container.get("events", [])
        elif isinstance(data, list):
            events = data
        else:
            events = container.get("events", [])
        if events is None:
            events = []
        if not isinstance(events, list):
            raise WatcherError("events/wait 响应的 events 不是数组")
        next_cursor = container.get("next_cursor")
        if next_cursor is None and container is not payload:
            next_cursor = payload.get("next_cursor")
        if next_cursor is None:
            pagination = container.get("pagination")
            if isinstance(pagination, Mapping):
                next_cursor = pagination.get("next_cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise WatcherError("events/wait 响应的 next_cursor 不是字符串")
        return events, next_cursor

    @staticmethod
    def _message_id(event: Any, index: int) -> str:
        if not isinstance(event, Mapping):
            raise WatcherError(f"events/wait 响应的 events[{index}] 不是对象")
        message_id = event.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise WatcherError(f"events/wait 响应的 events[{index}] 缺少 message_id")
        return message_id
