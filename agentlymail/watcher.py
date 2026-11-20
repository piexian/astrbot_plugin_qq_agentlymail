"""实现 events/wait 长轮询和邮件事件去重。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from typing import Any

from astrbot.api import logger

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
        trimmed = 0
        for message_id in seen_message_ids or ():
            trimmed += self._remember_seen(message_id)
        if trimmed:
            logger.debug(
                f"[agently] 邮件监听 初始化去重集合完成 裁剪数量={trimmed} "
                f"去重数量={len(self._seen_message_ids)} 上限={MAX_SEEN_MESSAGE_IDS}"
            )

    @property
    def next_cursor(self) -> str | None:
        """返回当前已确认的事件游标。"""
        return self._next_cursor

    @property
    def seen_message_ids(self) -> list[str]:
        """返回当前已确认的有序去重 ID 快照。"""
        return list(self._seen_message_ids)

    async def watch(self) -> AsyncIterator[Message]:
        """持续等待邮件事件并产出事件对应的完整邮件。"""
        delay = self.empty_delay
        logger.debug(
            f"[agently] 邮件监听 轮询启动 地址ID={self.alias_id} "
            f"游标存在={self._next_cursor is not None} "
            f"去重数量={len(self._seen_message_ids)}"
        )
        while True:
            logger.debug(
                f"[agently] 邮件监听 轮询开始 地址ID={self.alias_id} "
                f"游标存在={self._next_cursor is not None}"
            )
            payload = await self.client.events_wait(
                alias_id=self.alias_id,
                next_cursor=self._next_cursor,
            )
            events, response_cursor = self._events_and_cursor(payload)
            logger.debug(
                f"[agently] 邮件监听 轮询完成 地址ID={self.alias_id} "
                f"事件数量={len(events)} 响应游标存在={response_cursor is not None}"
            )
            if not events:
                if response_cursor is not None:
                    await self._persist_state(response_cursor, self.seen_message_ids)
                logger.debug(
                    f"[agently] 邮件监听 空轮询等待 地址ID={self.alias_id} 延迟={delay:.3f}s"
                )
                await self._sleep(delay)
                delay = min(max(delay * 2, self.empty_delay), self.max_empty_delay)
                logger.debug(
                    f"[agently] 邮件监听 空轮询退避更新 地址ID={self.alias_id} "
                    f"下次延迟={delay:.3f}s"
                )
                continue

            delay = self.empty_delay
            for index, event in enumerate(events):
                message_id = self._message_id(event, index)
                if message_id in self._seen_message_ids:
                    logger.debug(
                        f"[agently] 邮件监听 跳过重复事件 地址ID={self.alias_id} "
                        f"邮件ID={message_id}"
                    )
                    continue
                logger.debug(
                    f"[agently] 邮件监听 读取事件邮件 地址ID={self.alias_id} "
                    f"邮件ID={message_id}"
                )
                message = await self.client.read(message_id, alias_id=self.alias_id)
                logger.debug(
                    f"[agently] 邮件监听 邮件读取完成 地址ID={self.alias_id} "
                    f"邮件ID={message_id}"
                )
                candidate_seen = self._candidate_seen(message_id)
                logger.debug(
                    f"[agently] 邮件监听 产出邮件 地址ID={self.alias_id} "
                    f"邮件ID={message_id}"
                )
                yield message
                logger.debug(
                    f"[agently] 邮件监听 产出已恢复，准备确认 地址ID={self.alias_id} "
                    f"邮件ID={message_id}"
                )
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
        logger.debug(
            f"[agently] 邮件监听 持久化候选 地址ID={self.alias_id} "
            f"游标存在={next_cursor is not None} 去重数量={len(candidate_seen)}"
        )
        if self._save_state is not None:
            try:
                await self._save_state(next_cursor, candidate_seen)
            except Exception as exc:
                logger.debug(
                    f"[agently] 邮件监听 持久化失败 地址ID={self.alias_id} "
                    f"异常={type(exc).__name__}"
                )
                raise
            logger.debug(f"[agently] 邮件监听 持久化回调完成 地址ID={self.alias_id}")
        self._next_cursor = next_cursor
        self._seen_message_ids = OrderedDict.fromkeys(candidate_seen)
        logger.debug(
            f"[agently] 邮件监听 状态已提交 地址ID={self.alias_id} "
            f"游标存在={self._next_cursor is not None} "
            f"去重数量={len(self._seen_message_ids)}"
        )

    def _candidate_seen(self, message_id: str) -> list[str]:
        """生成加入一个新 ID 后的有界去重快照。"""
        candidate = OrderedDict.fromkeys(self._seen_message_ids)
        candidate.pop(message_id, None)
        candidate[message_id] = None
        trimmed = 0
        while len(candidate) > MAX_SEEN_MESSAGE_IDS:
            candidate.popitem(last=False)
            trimmed += 1
        if trimmed:
            logger.debug(
                f"[agently] 邮件监听 去重集合有界裁剪 地址ID={self.alias_id} "
                f"裁剪数量={trimmed} 上限={MAX_SEEN_MESSAGE_IDS}"
            )
        return list(candidate)

    def _remember_seen(self, message_id: str) -> int:
        """把初始去重 ID 加入有界顺序集合，返回裁剪数量。"""
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("seen_message_ids 必须是非空字符串数组")
        self._seen_message_ids.pop(message_id, None)
        self._seen_message_ids[message_id] = None
        trimmed = 0
        while len(self._seen_message_ids) > MAX_SEEN_MESSAGE_IDS:
            self._seen_message_ids.popitem(last=False)
            trimmed += 1
        return trimmed

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
