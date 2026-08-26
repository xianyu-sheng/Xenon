"""Python LSP tool family."""

from __future__ import annotations

from typing import Any

from xenon.engine.context import AgentContext


class LSPToolsMixin:
    """Expose Jedi-backed navigation through the legacy ToolNode contract."""

    def _lsp_goto_def(self, context: AgentContext) -> dict[str, Any]:
        """LSP: 跳转到定义。"""
        return self._lsp_call("goto_definition", context)

    def _lsp_find_refs(self, context: AgentContext) -> dict[str, Any]:
        """LSP: 查找引用。"""
        return self._lsp_call("find_references", context)

    def _lsp_hover(self, context: AgentContext) -> dict[str, Any]:
        """LSP: 悬停信息。"""
        return self._lsp_call("get_hover", context)

    def _lsp_diagnostics(self, context: AgentContext) -> dict[str, Any]:
        """LSP: 诊断信息。"""
        from xenon.utils.lsp_provider import LSPProvider

        file_path = self._resolve_template(self.file_path or "", context)
        if not file_path:
            return {"success": False, "error": "缺少 file_path 参数"}
        return LSPProvider.get_diagnostics(file_path)

    def _lsp_symbols(self, context: AgentContext) -> dict[str, Any]:
        """LSP: 文件符号列表。"""
        from xenon.utils.lsp_provider import LSPProvider

        file_path = self._resolve_template(self.file_path or "", context)
        if not file_path:
            return {"success": False, "error": "缺少 file_path 参数"}
        return LSPProvider.get_symbols(file_path)

    def _lsp_call(self, method: str, context: AgentContext) -> dict[str, Any]:
        """通用 LSP 调用分派。"""
        from xenon.utils.lsp_provider import LSPProvider

        file_path = self._resolve_template(self.file_path or "", context)
        if not file_path:
            return {"success": False, "error": "缺少 file_path 参数"}

        line = getattr(self, "_lsp_line", None)
        column = getattr(self, "_lsp_column", None)
        if line is None or column is None:
            return {"success": False, "error": "缺少 line 或 column 参数"}
        try:
            line = int(line)
            column = int(column)
        except (ValueError, TypeError):
            return {
                "success": False,
                "error": f"line/column 必须为整数: line={line}, column={column}",
            }

        if method == "goto_definition":
            return LSPProvider.goto_definition(file_path, line, column)
        if method == "find_references":
            return LSPProvider.find_references(file_path, line, column)
        if method == "get_hover":
            return LSPProvider.get_hover(file_path, line, column)
        return {"success": False, "error": f"未知 LSP 方法: {method}"}
