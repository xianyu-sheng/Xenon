"""CoreApp — omniagent-core 守护进程主应用。

管理所有运行时组件:
- SocketServer: TCP IPC 服务
- EventBus: 事件总线
- IpcEventBroadcaster: 事件→IPC 广播
- ToolRegistry: 工具注册中心
- AgentRunner: Agent 任务执行器
- SessionManager: 会话管理
- PermissionManager: 权限管理 (P1.4 增强)

借鉴 KamaClaude 的 CoreApp 设计:
- 命令处理器映射到 async handler
- 所有状态通过组件管理，不直接存储
- 事件流通过 EventBus + IPC 广播
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import omniagent

from omniagent.core.config import CoreConfig, get_core_config
from omniagent.core.transport.ipc_broadcaster import IpcEventBroadcaster
from omniagent.core.transport.socket_server import SocketServer
from omniagent.events.bus import EventBus
from omniagent.events.models import (
    LlmModelSelectedEvent,
    LlmTokenEvent,
    LlmUsageEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from omniagent.tools import ToolRegistry
from omniagent.tools.batch import BatchEditTool, BatchWriteTool
from omniagent.tools.code import AstAnalyzeTool, CodeIndexTool, DiffPreviewTool, RefactorTool
from omniagent.tools.command import CommandTool
from omniagent.tools.dynamic import RegisterTool
from omniagent.tools.file_ops import (
    CreateDirectoryTool,
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    WriteFileTool,
)
from omniagent.tools.mcp_tool import DateTimeTool, MCPCallTool
from omniagent.tools.search_git import GitTool, SearchFilesTool
from omniagent.tools.web import GithubFetchTool, WebFetchTool

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CoreApp:
    """omniagent-core 守护进程主应用。"""

    def __init__(self, config: CoreConfig | None = None) -> None:
        self._config = config or get_core_config()
        self._start_time = time.monotonic()

        # 核心组件
        self._bus = EventBus(name="core")
        self._server = SocketServer(
            host=self._config.host,
            port=self._config.port,
            max_connections=self._config.max_connections,
        )
        self._broadcaster = IpcEventBroadcaster(self._bus, self._server)
        self._tools = self._build_registry()

        # 运行时状态
        self._running_tasks: set[asyncio.Task[Any]] = set()

    @property
    def bus(self) -> EventBus:
        return self._bus

    @property
    def tools(self) -> ToolRegistry:
        return self._tools

    @property
    def uptime_ms(self) -> int:
        return int((time.monotonic() - self._start_time) * 1000)

    # ── 工具注册 ────────────────────────────────────────────

    @staticmethod
    def _build_registry() -> ToolRegistry:
        """构建默认工具注册表。"""
        registry = ToolRegistry()
        tools = [
            ReadFileTool(), WriteFileTool(), EditFileTool(),
            CreateDirectoryTool(), ListFilesTool(),
            CommandTool(), SearchFilesTool(), GitTool(),
            WebFetchTool(), GithubFetchTool(),
            BatchWriteTool(), BatchEditTool(),
            CodeIndexTool(), AstAnalyzeTool(),
            RefactorTool(), DiffPreviewTool(),
            MCPCallTool(), DateTimeTool(), RegisterTool(),
        ]
        for tool in tools:
            registry.register(tool)
        logger.info(f"ToolRegistry: {len(registry)} tools registered")
        return registry

    # ── 启动/停止 ───────────────────────────────────────────

    async def start(self) -> None:
        """启动 core daemon。"""
        logger.info(f"启动 omniagent-core v{omniagent.__version__} ...")

        # 设置服务器处理器
        self._server.on_request = self._handle_request
        self._server.on_notification = self._handle_notification
        self._server.on_connect = self._on_connect
        self._server.on_disconnect = self._on_disconnect

        # 启动 IPC 广播
        await self._broadcaster.start()

        # 启动服务器
        await self._server.start()
        logger.info(f"omniagent-core 已就绪: {self._config.host}:{self._config.port}")

    async def stop(self) -> None:
        """停止 core daemon。"""
        logger.info("停止 omniagent-core...")
        await self._broadcaster.stop()
        await self._server.stop()

        for task in self._running_tasks:
            task.cancel()
        await asyncio.gather(*self._running_tasks, return_exceptions=True)
        self._running_tasks.clear()
        logger.info("omniagent-core 已停止")

    # ── IPC 处理器 ──────────────────────────────────────────

    async def _on_connect(self, conn_id: int) -> None:
        logger.debug(f"客户端连接: conn={conn_id}")

    async def _on_disconnect(self, conn_id: int) -> None:
        self._broadcaster.remove_subscription(conn_id)

    async def _handle_request(
        self, method: str, params: dict[str, Any], conn_id: int,
    ) -> Any:
        """处理 JSON-RPC 请求，分发到对应的命令处理器。"""
        handlers: dict[str, Any] = {
            "core.ping": self._ping_handler,
            "agent.run": self._agent_run_handler,
            "event.subscribe": self._event_subscribe_handler,
            "session.create": self._session_create_handler,
            "session.send_message": self._session_send_message_handler,
            "session.get_history": self._session_get_history_handler,
            "session.close": self._session_close_handler,
            "set_model": self._set_model_handler,
        }

        handler = handlers.get(method)
        if not handler:
            raise ValueError(f"未知方法: {method}")

        return await handler(params, conn_id)

    async def _handle_notification(
        self, method: str, params: dict[str, Any], conn_id: int,
    ) -> None:
        """处理 JSON-RPC 通知。"""
        logger.debug(f"通知: {method} from conn={conn_id}")

    # ── 命令处理器 ──────────────────────────────────────────

    async def _ping_handler(self, params: dict[str, Any], conn_id: int) -> dict[str, Any]:
        client = params.get("client", "unknown")
        return {
            "server_version": omniagent.__version__,
            "uptime_ms": self.uptime_ms,
            "received_at": _now(),
        }

    async def _agent_run_handler(self, params: dict[str, Any], conn_id: int) -> dict[str, Any]:
        """启动一次 Agent 任务执行。"""
        goal = params.get("goal", "")
        mode = params.get("mode", "react")

        if not goal:
            raise ValueError("agent.run 需要 goal 参数")

        # 创建 run_id
        from omniagent.engine.run_recorder import new_run_id
        run_id = new_run_id()

        # 在后台任务中执行 agent
        task = asyncio.create_task(self._run_agent(run_id, goal, mode, conn_id))
        self._running_tasks.add(task)
        task.add_done_callback(self._running_tasks.discard)

        return {"run_id": run_id}

    async def _run_agent(
        self, run_id: str, goal: str, mode: str, conn_id: int,
    ) -> None:
        """后台执行 Agent 任务。"""
        from omniagent.engine.context import AgentContext
        from omniagent.engine.react_engine import ReActEngine
        from omniagent.events.callbacks_bridge import EventAwareCallback
        from omniagent.engine.callbacks import ConsoleCallback

        # 发布 run started 事件
        await self._bus.publish(RunStartedEvent(
            run_id=run_id, goal=goal, mode=mode, model_ids=[], cwd=str(Path.cwd()),
        ))

        try:
            callback = EventAwareCallback(ConsoleCallback(), self._bus)
            callback.set_run_id(run_id)

            context = AgentContext()
            engine = ReActEngine(
                model_priority=["deepseek/deepseek-v4-pro"],
                max_iterations=15,
                callback=callback,
            )
            result = engine.run(goal, context)
            await self._bus.publish(RunFinishedEvent(run_id=run_id, status="success", result=result))
        except Exception as e:
            await self._bus.publish(RunFinishedEvent(run_id=run_id, status="error", reason=str(e)))

    async def _event_subscribe_handler(self, params: dict[str, Any], conn_id: int) -> dict[str, Any]:
        """处理事件订阅请求。"""
        topics = params.get("topics", [])
        self._broadcaster.add_subscription(conn_id, topics)
        return {"subscription_id": f"sub-{conn_id}"}

    async def _session_create_handler(self, params: dict[str, Any], conn_id: int) -> dict[str, Any]:
        # 简化实现: 返回假 session_id
        import uuid
        return {"session_id": f"sess-{uuid.uuid4().hex[:8]}"}

    async def _session_send_message_handler(self, params: dict[str, Any], conn_id: int) -> dict[str, Any]:
        # 委托给 agent.run
        return await self._agent_run_handler({
            "goal": params.get("content", ""),
            "mode": params.get("mode", "react"),
        }, conn_id)

    async def _session_get_history_handler(self, params: dict[str, Any], conn_id: int) -> dict[str, Any]:
        return {"messages": []}

    async def _session_close_handler(self, params: dict[str, Any], conn_id: int) -> dict[str, Any]:
        return {"status": "closed"}

    async def _set_model_handler(self, params: dict[str, Any], conn_id: int) -> dict[str, Any]:
        return {"ok": True, "current_models": params.get("model_ids", [])}


# ── 入口 ────────────────────────────────────────────────────

def run() -> None:
    """kama-core 入口点。"""
    config = get_core_config()
    app = CoreApp(config)

    try:
        asyncio.run(app.start())
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在停止...")
        asyncio.run(app.stop())
