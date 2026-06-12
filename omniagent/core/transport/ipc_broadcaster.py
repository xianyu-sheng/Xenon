"""IPC 事件广播器 — 将 EventBus 事件推送到连接的客户端。

每当 EventBus 发布事件时，IPC 广播器自动将事件转发给所有订阅了对应 topic 的连接。
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any

from pydantic import BaseModel

from omniagent.events.bus import EventBus
from omniagent.core.transport.socket_server import SocketServer

logger = logging.getLogger(__name__)


class IpcEventBroadcaster:
    """将 EventBus 事件广播到 IPC 客户端。

    订阅 EventBus，当事件到达时:
    1. 检查每个连接的订阅 topic 列表
    2. 使用 fnmatch 匹配事件类型
    3. 匹配的连接收到推送事件（以 "event.<type>" 通知格式）
    """

    def __init__(self, bus: EventBus, server: SocketServer) -> None:
        self._bus = bus
        self._server = server
        # conn_id -> list[topic_pattern]
        self._subscriptions: dict[int, list[str]] = {}
        self._subscribed = False

    async def start(self) -> None:
        """开始监听 EventBus 并广播事件。"""
        if self._subscribed:
            return
        self._subscribed = True
        self._bus.subscribe(self._on_event)
        logger.info("IPC 事件广播器已启动")

    async def stop(self) -> None:
        """停止广播。"""
        self._bus.unsubscribe(self._on_event)
        self._subscribed = False

    def add_subscription(self, conn_id: int, topics: list[str]) -> None:
        """为连接添加事件订阅。"""
        self._subscriptions[conn_id] = topics
        logger.debug(f"连接 conn={conn_id} 订阅 topics={topics}")

    def remove_subscription(self, conn_id: int) -> None:
        """移除连接的订阅。"""
        self._subscriptions.pop(conn_id, None)

    async def _on_event(self, event: BaseModel) -> None:
        """EventBus 事件处理 — 广播给匹配的客户端。"""
        event_type = getattr(event, "event_type", type(event).__name__)
        event_dict = event.model_dump()

        for conn_id, topics in list(self._subscriptions.items()):
            if self._matches(event_type, topics):
                try:
                    await self._server.send_event(conn_id, {
                        "type": f"event.{event_type}",
                        "data": event_dict,
                    })
                except Exception as e:
                    logger.debug(f"广播事件到 conn={conn_id} 失败: {e}")

    @staticmethod
    def _matches(event_type: str, topics: list[str]) -> bool:
        """检查事件类型是否匹配任一 topic 模式。"""
        if not topics:
            return True  # 空列表 = 接收所有
        return any(fnmatch.fnmatch(event_type, t) for t in topics)
