"""ToolRegistry — 工具注册中心。

统一管理所有 BaseTool 实例，提供注册、查找、schema 导出等功能。
替代原有 ToolNode 中的硬编码工具分发逻辑。
"""

from __future__ import annotations

import logging
from typing import Any

from omniagent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class ToolRegistry:
    """工具注册中心。

    使用方式:
        registry = ToolRegistry()
        registry.register(ReadFileTool())
        tool = registry.get("read_file")
        result = await tool.invoke({"path": "app.py"})
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._dynamic_tools: dict[str, BaseTool] = {}

    # ── 注册 / 注销 ──────────────────────────────────────────

    def register(self, tool: BaseTool) -> None:
        """注册工具，同名覆盖。"""
        if not tool.name:
            raise ValueError("Tool must have a non-empty name")
        self._tools[tool.name] = tool
        logger.debug(f"Tool registered: {tool.name}")

    def register_dynamic(self, tool: BaseTool) -> None:
        """注册动态工具（运行时添加，与内置工具分开管理）。"""
        if not tool.name:
            raise ValueError("Dynamic tool must have a non-empty name")
        self._dynamic_tools[tool.name] = tool
        logger.info(f"Dynamic tool registered: {tool.name}")

    def unregister(self, name: str) -> bool:
        """注销工具，返回是否成功。"""
        if name in self._tools:
            del self._tools[name]
            return True
        if name in self._dynamic_tools:
            del self._dynamic_tools[name]
            return True
        return False

    # ── 查找 ────────────────────────────────────────────────

    def get(self, name: str) -> BaseTool | None:
        """按名称查找工具（先查内置，再查动态）。"""
        return self._tools.get(name) or self._dynamic_tools.get(name)

    def list_all(self) -> dict[str, BaseTool]:
        """列出所有工具（内置 + 动态）。"""
        return {**self._tools, **self._dynamic_tools}

    def list_names(self) -> list[str]:
        """列出所有工具名。"""
        return list(self._tools.keys()) + list(self._dynamic_tools.keys())

    # ── Schema 导出 ──────────────────────────────────────────

    def tool_schemas(self) -> list[dict[str, object]]:
        """返回所有工具的 Anthropic 格式 schema 列表。"""
        return [tool.to_schema() for tool in self.list_all().values()]

    def format_for_prompt(self) -> str:
        """将所有工具格式化为 LLM 提示词文本。"""
        lines = []
        for name, tool in self.list_all().items():
            params_desc = ", ".join(
                f"{k}: {v.get('type', 'any')}"
                for k, v in tool.input_schema.get("properties", {}).items()
            )
            lines.append(f"- {name}: {tool.description} (参数: {params_desc})")
        return "\n".join(lines) if lines else "(无可用工具)"

    # ── 执行 ────────────────────────────────────────────────

    async def invoke(self, name: str, params: dict[str, object]) -> ToolResult:
        """调用工具并返回 ToolResult。

        Args:
            name: 工具名
            params: 工具参数

        Returns:
            ToolResult 执行结果
        """
        tool = self.get(name)
        if tool is None:
            return ToolResult.error(
                f"未知工具 '{name}'，可用工具: {self.list_names()}",
                error_type="schema_error",
            )

        try:
            return await tool.invoke(params)
        except Exception as e:
            logger.error(f"Tool '{name}' execution error: {e}", exc_info=True)
            return ToolResult.error(str(e), error_type="runtime_error")

    def __len__(self) -> int:
        return len(self._tools) + len(self._dynamic_tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools or name in self._dynamic_tools
