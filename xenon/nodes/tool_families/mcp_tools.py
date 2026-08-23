"""MCP tool family."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xenon.engine.context import AgentContext


class MCPToolsMixin:
    """Call a configured MCP registry through the ToolNode contract."""

    def _validate_mcp_paths(self, args: dict[str, Any], context: AgentContext) -> str | None:
        """验证 MCP 工具参数中的文件路径。

        Args:
            args: MCP 工具参数字典
            context: 执行上下文

        Returns:
            错误消息字符串，如果验证通过则返回 None

        Note:
            MCP 服务器是外部进程，此验证仅能拦截常见的路径参数名。
            恶意 MCP 服务器可以通过自定义参数名或编码路径绕过此检查。
        """
        # 常见的文件路径参数名
        PATH_PARAM_NAMES = {
            'path', 'file_path', 'filepath', 'file', 'filename',
            'dir', 'directory', 'folder', 'uri', 'url',
            'source', 'destination', 'target', 'output',
            'input_path', 'output_path', 'src', 'dst'
        }

        # 检查是否有 _validate_path 方法（来自 ToolNode）
        if not hasattr(self, '_validate_path'):
            # 如果没有路径验证能力（不应该发生），跳过检查
            return None

        for key, value in args.items():
            # 只检查字符串值且参数名看起来像路径
            if not isinstance(value, str):
                continue

            key_lower = key.lower()
            if not any(path_key in key_lower for path_key in PATH_PARAM_NAMES):
                continue

            # 跳过明显不是文件路径的值（URL、相对路径等）
            if value.startswith(('http://', 'https://', 'file://', 'ftp://')):
                continue
            if not value or len(value) > 500:  # 路径不应该太长
                continue

            # 尝试验证路径
            try:
                # 推断是否为写入操作（基于工具名和参数名）
                tool_name = self.tool_name.lower() if hasattr(self, 'tool_name') else ''
                is_write = any(keyword in tool_name or keyword in key_lower
                             for keyword in ['write', 'save', 'create', 'edit',
                                           'delete', 'remove', 'update', 'modify'])

                # 调用 ToolNode 的路径验证
                self._validate_path(value, for_write=is_write)
            except Exception as e:
                # 路径验证失败
                return f"MCP 工具路径验证失败 (参数 '{key}'): {e}"

        return None

    def _mcp_call(self, context: AgentContext) -> dict[str, Any]:
        """调用 MCP 服务器工具。"""

        tool_name = self._resolve_template(self.tool_name, context)
        if not tool_name:
            return {
                "action_type": "mcp_call",
                "tool": tool_name,
                "success": False,
                "error": "需要 tool_name 参数",
                "content": None,
                "metadata": None,
            }

        # 获取注册表（从 context 或创建新的）
        registry = context.get("_mcp_registry")
        if not registry:
            return {
                "action_type": "mcp_call",
                "tool": tool_name,
                "success": False,
                "error": "MCP 未初始化。请先使用 /mcp add 命令添加 MCP 服务器",
                "content": None,
                "metadata": None,
            }

        try:
            # 解析参数中的模板
            args = {}
            for k, v in self.tool_args.items():
                if isinstance(v, str):
                    args[k] = self._resolve_template(v, context)
                else:
                    args[k] = v

            # P0 安全：验证 MCP 工具参数中的文件路径
            # MCP 服务器是外部进程，我们无法拦截其内部文件操作，
            # 但可以扫描常见的路径参数名并提前验证
            path_validation_error = self._validate_mcp_paths(args, context)
            if path_validation_error:
                return {
                    "action_type": "mcp_call",
                    "tool": tool_name,
                    "success": False,
                    "error": path_validation_error,
                    "content": None,
                    "metadata": None,
                }

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
                "content": None,
                "metadata": None,
            }

