"""MCP tool family."""

from __future__ import annotations

from typing import Any

from xenon.engine.context import AgentContext


class MCPToolsMixin:
    """Call a configured MCP registry through the ToolNode contract."""

    def _mcp_call(self, context: AgentContext) -> dict[str, Any]:
        """调用 MCP 服务器工具。"""

        tool_name = self._resolve_template(self.tool_name, context)
        if not tool_name:
            return {
                "action_type": "mcp_call",
                "success": False,
                "error": "需要 tool_name 参数",
            }

        # 获取注册表（从 context 或创建新的）
        registry = context.get("_mcp_registry")
        if not registry:
            return {
                "action_type": "mcp_call",
                "success": False,
                "error": "MCP 未初始化。请先使用 /mcp add 命令添加 MCP 服务器",
            }

        try:
            # 解析参数中的模板
            args = {}
            for k, v in self.tool_args.items():
                if isinstance(v, str):
                    args[k] = self._resolve_template(v, context)
                else:
                    args[k] = v

            result = registry.call_tool(tool_name, args)

            # 提取结果内容
            content_parts = []
            for item in result.get("content", []):
                if item.get("type") == "text":
                    content_parts.append(item.get("text", ""))
                else:
                    content_parts.append(str(item))

            display = "\n".join(content_parts) if content_parts else str(result)
            display, filter_meta = self._prefilter_result_text(display, context)
            display_cap = 12000 if filter_meta else 5000
            display = display[:display_cap]
            self._write_output(context, display)

            return {
                "action_type": "mcp_call",
                "tool": tool_name,
                "result": result,
                "content": display,  # v0.5.3: LLM 可读的文本输出
                "success": True,
                **filter_meta,
            }

        except Exception as e:
            return {
                "action_type": "mcp_call",
                "tool": tool_name,
                "success": False,
                "error": str(e),
            }

