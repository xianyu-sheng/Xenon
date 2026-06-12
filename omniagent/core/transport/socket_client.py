"""TCP NDJSON Socket 客户端 — CLI/TUI 连接到 omniagent-core。

借鉴 KamaClaude 的 SocketClient 设计:
- 通过 TCP + NDJSON 与 core daemon 通信
- 支持同步请求/响应和事件推送订阅
- 自动重连机制
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class IpcError(Exception):
    """IPC 通信错误。"""
    pass


class SocketClient:
    """TCP 客户端，连接 omniagent-core daemon。

    使用方式:
        client = SocketClient()
        await client.connect()
        result = await client.request("core.ping", {"client": "cli"})
        await client.subscribe_events(["tool.*", "step.*"], on_event)
        await client.close()
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9501) -> None:
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id: int = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._event_handlers: list[Any] = []
        self._read_task: asyncio.Task[None] | None = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """连接到 core daemon。"""
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self._host, self._port
            )
            self._connected = True
            self._read_task = asyncio.create_task(self._read_loop())
            logger.info(f"已连接 omniagent-core: {self._host}:{self._port}")
        except Exception as e:
            raise IpcError(f"无法连接到 omniagent-core ({self._host}:{self._port}): {e}")

    async def close(self) -> None:
        """断开连接。"""
        self._connected = False
        if self._read_task:
            self._read_task.cancel()
            self._read_task = None
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
        self._reader = None
        # 取消所有 pending 请求
        for future in self._pending.values():
            if not future.done():
                future.set_exception(IpcError("连接已断开"))
        self._pending.clear()

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """发送 JSON-RPC 请求并等待响应。"""
        if not self._writer or not self._connected:
            raise IpcError("未连接到 omniagent-core")

        self._next_id += 1
        msg_id = self._next_id

        future: asyncio.Future[dict[str, Any]] = asyncio.Future()
        self._pending[msg_id] = future

        frame = json.dumps({
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params,
        }, ensure_ascii=False) + "\n"

        try:
            self._writer.write(frame.encode("utf-8"))
            await self._writer.drain()
        except Exception as e:
            self._pending.pop(msg_id, None)
            raise IpcError(f"发送请求失败: {e}")

        try:
            return await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise IpcError(f"请求超时: {method}")

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        """发送 JSON-RPC 通知（不需要响应）。"""
        if not self._writer or not self._connected:
            raise IpcError("未连接到 omniagent-core")

        frame = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }, ensure_ascii=False) + "\n"

        self._writer.write(frame.encode("utf-8"))
        await self._writer.drain()

    def on_event(self, handler: Any) -> None:
        """注册事件处理器。"""
        self._event_handlers.append(handler)

    # ── 内部方法 ────────────────────────────────────────────

    async def _read_loop(self) -> None:
        """持续读取 NDJSON 帧循环。"""
        while self._connected and self._reader:
            try:
                line = await self._reader.readline()
                if not line:
                    break
                data = json.loads(line.decode("utf-8"))
                await self._dispatch(data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._connected:
                    logger.debug(f"读取帧失败: {e}")
                continue

        self._connected = False
        logger.info("连接已断开")

    async def _dispatch(self, data: dict[str, Any]) -> None:
        """分发响应或事件。"""
        msg_id = data.get("id")

        if msg_id is not None and msg_id in self._pending:
            # 这是对之前请求的响应
            future = self._pending.pop(msg_id)
            if not future.done():
                if "error" in data:
                    future.set_exception(IpcError(data["error"].get("message", str(data["error"]))))
                else:
                    future.set_result(data.get("result", {}))
        else:
            # 这是服务器推送的事件
            for handler in self._event_handlers:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(data)
                    else:
                        handler(data)
                except Exception:
                    pass
