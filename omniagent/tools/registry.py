"""ToolRegistry — 工具注册中心。

统一管理所有工具实例（BaseTool / 内置同步工具 / 动态工具），
提供注册、查找、schema 导出、执行等功能。
替代原有 ToolNode 中的硬编码工具分发逻辑。
"""

from __future__ import annotations

import logging
from typing import Any

from omniagent.engine.context import AgentContext
from omniagent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class ToolRegistry:
    """工具注册中心 — 取代 ToolNode 硬编码分发。

    支持三种工具类型:
    1. 内置同步工具 (builtin/*.py) — execute(context) -> dict
    2. BaseTool 异步工具 — await invoke(params) -> ToolResult
    3. 动态工具 — 运行时注册的 handler

    使用方式:
        registry = get_registry()          # 全局单例，自动注册所有内置工具
        result = registry.execute_sync("read_file", {"file_path": "app.py"}, context)
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._sync_tools: dict[str, type] = {}  # 内置同步工具类
        self._dynamic_tools: dict[str, dict] = {}  # {handler, description, params}
        self._init_builtins()

    def _init_builtins(self) -> None:
        """自动注册所有内置同步工具。"""
        from omniagent.tools.builtin.file_tools import (
            CopyFileTool, CreateDirectoryTool, EditFileTool, ListFilesTool,
            MoveFileTool, ReadFileTool, SearchFilesTool, WriteFileTool,
        )
        from omniagent.tools.builtin.exec_tools import CommandTool, GitTool
        from omniagent.tools.builtin.web_tools import GitHubFetchTool, WeatherTool, WebFetchTool
        from omniagent.tools.builtin.code_tools import (
            AstAnalyzeTool, CodeIndexTool, DiffPreviewTool, RefactorTool,
        )
        from omniagent.tools.builtin.batch_tools import BatchEditTool, BatchWriteTool
        from omniagent.tools.builtin.meta_tools import DateTimeTool, MCPCallTool, RegisterToolTool
        from omniagent.tools.builtin.subagent_tools import AgentResultTool, SpawnAgentTool
        from omniagent.tools.builtin.remember_tool import RememberTool

        _builtins: list[type] = [
            ReadFileTool, WriteFileTool, EditFileTool, ListFilesTool,
            SearchFilesTool, CreateDirectoryTool, MoveFileTool, CopyFileTool,
            CommandTool, GitTool,
            WebFetchTool, GitHubFetchTool, WeatherTool,
            CodeIndexTool, AstAnalyzeTool, RefactorTool, DiffPreviewTool,
            BatchWriteTool, BatchEditTool,
            MCPCallTool, RegisterToolTool, DateTimeTool,
            SpawnAgentTool, AgentResultTool,
            RememberTool,
        ]
        for tool_cls in _builtins:
            if tool_cls.name:
                self._sync_tools[tool_cls.name] = tool_cls

    # ── 注册 / 注销 ──────────────────────────────────────────

    def register(self, tool: BaseTool) -> None:
        """注册 BaseTool 实例。"""
        if not tool.name:
            raise ValueError("Tool must have a non-empty name")
        self._tools[tool.name] = tool
        logger.debug(f"Tool registered: {tool.name}")

    def register_dynamic(self, name: str, handler, description: str, params: dict) -> None:
        """注册动态工具（运行时通过 register_tool 创建）。"""
        self._dynamic_tools[name] = {"handler": handler, "description": description, "params": params}
        logger.info(f"Dynamic tool registered: {name}")

    def unregister(self, name: str) -> bool:
        if name in self._tools:
            del self._tools[name]
            return True
        if name in self._sync_tools:
            del self._sync_tools[name]
            return True
        if name in self._dynamic_tools:
            del self._dynamic_tools[name]
            return True
        return False

    # ── 查找 ────────────────────────────────────────────────

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def get_sync_class(self, name: str) -> type | None:
        """获取内置同步工具类。"""
        return self._sync_tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools or name in self._sync_tools or name in self._dynamic_tools

    def list_names(self) -> list[str]:
        return list(self._tools.keys()) + list(self._sync_tools.keys()) + list(self._dynamic_tools.keys())

    def list_all(self) -> dict[str, BaseTool]:
        return dict(self._tools)

    # ── Schema ──────────────────────────────────────────────

    def tool_schemas(self) -> list[dict[str, object]]:
        return [t.to_schema() for t in self._tools.values()]

    def format_for_prompt(self) -> str:
        lines = []
        for name in self._sync_tools:
            lines.append(f"- {name}")
        for name, tool in self._tools.items():
            lines.append(f"- {name}: {tool.description}")
        for name, info in self._dynamic_tools.items():
            lines.append(f"- {name}: {info.get('description', '')}")
        return "\n".join(lines) if lines else "(无可用工具)"

    def tool_descriptions(self) -> dict[str, dict]:
        """返回所有工具的 {name: {description, params}} 供引擎使用。"""
        result = {}
        for name, tool_cls in self._sync_tools.items():
            desc = getattr(tool_cls, "description", None) or f"内置工具: {name}"
            params = getattr(tool_cls, "params", None) or {}
            result[name] = {"name": name, "description": desc, "params": params}
        for name, tool in self._tools.items():
            result[name] = {"name": name, "description": tool.description, "params": tool.input_schema.get("properties", {})}
        for name, info in self._dynamic_tools.items():
            result[name] = {"name": name, "description": info.get("description", ""), "params": info.get("params", {})}
        return result

    # ── 执行 ────────────────────────────────────────────────

    async def invoke(self, name: str, params: dict[str, object]) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult.error(f"未知工具 '{name}'，可用: {self.list_names()}", error_type="schema_error")
        try:
            return await tool.invoke(params)
        except Exception as e:
            logger.error(f"Tool '{name}' execution error: {e}", exc_info=True)
            return ToolResult.error(str(e), error_type="runtime_error")

    def execute_sync(self, name: str, params: dict, context: AgentContext, **kwargs) -> dict[str, Any]:
        """同步执行工具 — 替代 ToolNode.execute()。

        先查内置同步工具类，再查 BaseTool 实例，最后查动态工具。
        """
        # 内置同步工具
        tool_cls = self._sync_tools.get(name)
        if tool_cls:
            tool = tool_cls(**kwargs, **params)
            return tool.execute(context)

        # BaseTool 实例（包装为同步调用）
        if name in self._tools:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import nest_asyncio
                    nest_asyncio.apply()
                result = asyncio.run(self.invoke(name, params))
            except RuntimeError:
                result = asyncio.run(self.invoke(name, params))
            return {
                "action_type": name, "success": not result.is_error,
                "content": result.content, "error": result.content if result.is_error else None,
            }

        # 动态工具
        dynamic = self._dynamic_tools.get(name)
        if dynamic:
            try:
                result = dynamic["handler"](context)
                return result if isinstance(result, dict) else {"action_type": name, "success": True, "content": str(result)}
            except Exception as e:
                return {"action_type": name, "success": False, "error": str(e)}

        raise ValueError(f"未知工具: '{name}'，可用: {self.list_names()}")

    def __len__(self) -> int:
        return len(self._tools) + len(self._sync_tools) + len(self._dynamic_tools)

    def __contains__(self, name: str) -> bool:
        return self.has(name)


# 全局单例
_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry

