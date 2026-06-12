"""TCP NDJSON Socket 服务器 — 实现 omniagent-core IPC 传输层。

借鉴 KamaClaude 的 TCP + NDJSON 设计:
- 每个连接一行 JSON（NDJSON 格式）
- 支持 JSON-RPC 2.0 请求/响应/通知
- 异步 I/O（asyncio）
- 连接管理（最大连接数限制）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

MAX_FRAME_BYTES = 10 * 1024 * 1024  # 10MB 单帧上限


class SocketServer:
    """异步 TCP 服务器，用于 core<->client IPC。

    使用方式:
        server = SocketServer(host="127.0.0.1", port=9501)
        server.on_request = my_handler  # async (params) -> result
        server.on_notification = my_notify_handler
        await server.start()
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9501,
        *,
        max_connections: int = 10,
    ) -> None:
        self._host = host
        self._port = port
        self._max_connections = max_connections
        self._server: asyncio.Server | None = None
        self._connections: dict[int, asyncio.StreamWriter] = {}
        self._next_conn_id: int = 0
        self._start_time = time.monotonic()

        # 请求处理器
        self.on_request: Any = None  # async (method, params, conn_id) -> result
        self.on_notification: Any = None  # async (method, params) -> None
        self.on_connect: Any = None  # async (conn_id) -> None
        self.on_disconnect: Any = None  # async (conn_id) -> None

    @property
    def uptime_ms(self) -> int:
        return int((time.monotonic() - self._start_time) * 1000)

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def start(self) -> None:
        """启动服务器。"""
        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self._host,
            port=self._port,
        )
        logger.info(f"SocketServer 启动: {self._host}:{self._port}")

    async def stop(self) -> None:
        """停止服务器。"""
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        for writer in list(self._connections.values()):
            try:
                writer.close()
            except Exception:
                pass
        self._connections.clear()
        logger.info("SocketServer 已停止")

    async def send_event(self, conn_id: int, event_data: dict[str, Any]) -> None:
        """向指定连接发送事件。"""
        writer = self._connections.get(conn_id)
        if writer:
            try:
                await self._send_frame(writer, event_data)
            except Exception as e:
                logger.warning(f"发送事件到 conn={conn_id} 失败: {e}")

    async def broadcast(self, event_data: dict[str, Any], *, exclude: int | None = None) -> None:
        """向所有连接广播事件。"""
        for conn_id, writer in list(self._connections.items()):
            if conn_id == exclude:
                continue
            try:
                await self._send_frame(writer, event_data)
            except Exception:
                pass

    # ── 连接处理 ────────────────────────────────────────────

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        self._next_conn_id += 1
        conn_id = self._next_conn_id
        self._connections[conn_id] = writer

        addr = writer.get_extra_info("peername", ("?", 0))
        logger.info(f"新连接 conn={conn_id} from {addr} (total={len(self._connections)})")

        if self.on_connect:
            try:
                await self.on_connect(conn_id)
            except Exception:
                pass

        try:
            await self._read_loop(reader, writer, conn_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"连接 conn={conn_id} 异常: {e}")
        finally:
            self._connections.pop(conn_id, None)
            if self.on_disconnect:
                try:
                    await self.on_disconnect(conn_id)
                except Exception:
                    pass
            try:
                writer.close()
            except Exception:
                pass
            logger.info(f"连接关闭 conn={conn_id} (total={len(self._connections)})")

    async def _read_loop(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, conn_id: int,
    ) -> None:
        """读取 NDJSON 帧循环。"""
        while True:
            line = await reader.readline()
            if not line:
                break  # 连接关闭

            if len(line) > MAX_FRAME_BYTES:
                logger.warning(f"帧过大 conn={conn_id}: {len(line)} 字节")
                continue

            try:
                data = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as e:
                logger.warning(f"JSON 解析失败 conn={conn_id}: {e}")
                await self._send_frame(writer, {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {e}"},
                })
                continue

            await self._dispatch(data, writer, conn_id)

    async def _dispatch(
        self, data: dict[str, Any], writer: asyncio.StreamWriter, conn_id: int,
    ) -> None:
        """分发 JSON-RPC 消息。"""
        msg_id = data.get("id")
        method = data.get("method", "")

        # 通知（无 id，不需要响应）
        if msg_id is None and method:
            if self.on_notification:
                try:
                    await self.on_notification(method, data.get("params", {}), conn_id)
                except Exception as e:
                    logger.warning(f"处理通知 '{method}' 失败: {e}")
            return

        # 请求
        if method and self.on_request:
            try:
                result = await self.on_request(method, data.get("params", {}), conn_id)
                await self._send_frame(writer, {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": result.model_dump() if hasattr(result, "model_dump") else result,
                })
            except Exception as e:
                logger.error(f"处理请求 '{method}' 失败: {e}", exc_info=True)
                await self._send_frame(writer, {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32603, "message": str(e)},
                })

    @staticmethod
    async def _send_frame(writer: asyncio.StreamWriter, data: dict[str, Any]) -> None:
        """发送 NDJSON 帧。"""
        frame = json.dumps(data, ensure_ascii=False, default=str) + "\n"
        writer.write(frame.encode("utf-8"))
        await writer.drain()
