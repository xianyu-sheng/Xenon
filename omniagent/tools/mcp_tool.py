"""MCP 工具调用 — MCPCallTool。"""

from __future__ import annotations

import json
import logging
from typing import Any

from omniagent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class MCPCallTool(BaseTool):
    name = "mcp_call"
    description = "调用通过 MCP 协议连接的外部工具服务器。需要先用 /mcp add 命令添加服务器并发现可用工具。"
    input_schema = {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string", "description": "MCP 工具名，格式为 server:tool 或 tool"},
            "tool_args": {"type": "object", "description": "工具参数字典"},
        },
        "required": ["tool_name"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        tool_name = str(params.get("tool_name", ""))
        tool_args: dict[str, Any] = {}

        raw_args = params.get("tool_args", {})
        if isinstance(raw_args, str):
            try:
                tool_args = json.loads(raw_args)
            except json.JSONDecodeError:
                tool_args = {}
        elif isinstance(raw_args, dict):
            tool_args = dict(raw_args)

        if not tool_name:
            return ToolResult.schema_error("mcp_call 需要 tool_name 参数")

        try:
            from omniagent.mcp.registry import MCPRegistry
            registry = MCPRegistry()
            result = registry.call_tool(tool_name, tool_args)

            content_parts = []
            for item in result.get("content", []):
                if isinstance(item, dict) and item.get("type") == "text":
                    content_parts.append(item.get("text", ""))
                else:
                    content_parts.append(str(item))

            display = "\n".join(content_parts)[:5000] if content_parts else str(result)[:5000]
            return ToolResult.ok(display, tool_name=tool_name)

        except ImportError:
            return ToolResult.error("MCP 未初始化。请先使用 /mcp add 命令添加 MCP 服务器")
        except Exception as e:
            return ToolResult.error(f"MCP 调用失败: {e}")


class DateTimeTool(BaseTool):
    name = "datetime"
    description = "获取当前日期和时间信息，包括年月日、星期几、时分秒。当用户询问时间相关问题时使用此工具。"
    input_schema = {
        "type": "object",
        "properties": {},
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        from datetime import datetime
        now = datetime.now()
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

        content = (
            f"📅 当前日期: {now.year}年{now.month}月{now.day}日 {weekdays[now.weekday()]}\n"
            f"🕐 当前时间: {now.strftime('%H:%M:%S')}\n"
            f"📊 详细信息:\n"
            f"  - 年: {now.year}, 月: {now.month}, 日: {now.day}\n"
            f"  - 时: {now.hour}, 分: {now.minute}, 秒: {now.second}"
        )
        return ToolResult.ok(
            content,
            year=now.year, month=now.month, day=now.day,
            weekday=weekdays[now.weekday()],
        )
