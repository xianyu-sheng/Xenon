"""EventBus — 核心发布-订阅事件总线。

轻量级异步事件总线，设计对齐 KamaClaude 的 EventBus:
- 基于 Pydantic BaseModel 的类型化事件
- async handler 订阅模式
- 按注册顺序依次调用订阅者
- 任何组件都可以独立注册 handler

使用方式:
    bus = EventBus()
    bus.subscribe(my_handler)
    await bus.publish(ToolCallStartedEvent(...))
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from omniagent.events.models import BaseEvent

logger = logging.getLogger(__name__)

# 事件处理函数类型
EventHandler = Callable[[BaseEvent], Awaitable[None]]


class EventBus:
    """异步发布-订阅事件总线。

    所有订阅者是 async 函数，publish 按注册顺序依次 await 每个订阅者。
    单个订阅者异常不会中断其他订阅者。
    """

    def __init__(self, *, name: str = "default") -> None:
        self._subscribers: list[EventHandler] = []
        self._name = name
        self._event_count: int = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def event_count(self) -> int:
        return self._event_count

    def subscribe(self, handler: EventHandler) -> None:
        """注册事件处理函数。

        Args:
            handler: async 函数，接收 BaseModel 事件
        """
        if handler not in self._subscribers:
            self._subscribers.append(handler)
            logger.debug(f"EventBus[{self._name}]: subscriber added (total={len(self._subscribers)})")

    def unsubscribe(self, handler: EventHandler) -> None:
        """取消注册事件处理函数。"""
        if handler in self._subscribers:
            self._subscribers.remove(handler)
            logger.debug(f"EventBus[{self._name}]: subscriber removed (total={len(self._subscribers)})")

    async def publish(self, event: BaseModel) -> None:
        """发布事件给所有订阅者。

        按注册顺序依次调用每个 handler。单个 handler 异常会被捕获并记录，
        不会中断其他 handler 的执行。

        Args:
            event: 任意 Pydantic BaseModel 实例
        """
        self._event_count += 1
        event_type = getattr(event, "event_type", type(event).__name__)

        for i, handler in enumerate(self._subscribers):
            try:
                await handler(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    f"EventBus[{self._name}]: handler #{i} failed for event {event_type}",
                    exc_info=True,
                )

    async def publish_parallel(self, event: BaseModel) -> None:
        """并行发布事件给所有订阅者（不保证顺序）。

        使用 asyncio.gather 同时执行所有 handler。
        单个 handler 异常不会中断其他 handler。
        """
        self._event_count += 1
        event_type = getattr(event, "event_type", type(event).__name__)

        async def _safe(handler: EventHandler) -> None:
            try:
                await handler(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    f"EventBus[{self._name}]: parallel handler failed for event {event_type}",
                    exc_info=True,
                )

        await asyncio.gather(*(_safe(h) for h in self._subscribers))

    def publish_sync(self, event: BaseModel) -> None:
        """同步发布事件（从同步 callback 中调用）。

        尝试获取当前 event loop，如果运行中则创建 task，
        否则用 asyncio.run 执行。事件总线本身是 fire-and-forget。
        """
        try:
            loop = asyncio.get_running_loop()
            # 在 async 上下文中，创建 task 发布
            loop.create_task(self.publish(event))
        except RuntimeError:
            # 无运行中的 loop，使用 asyncio.run
            try:
                asyncio.run(self.publish(event))
            except Exception:
                pass  # 最佳努力，事件丢失

    def clear(self) -> None:
        """清除所有订阅者。"""
        self._subscribers.clear()
        logger.debug(f"EventBus[{self._name}]: all subscribers cleared")

    def __repr__(self) -> str:
        return f"EventBus(name={self._name!r}, subscribers={len(self._subscribers)}, events={self._event_count})"
