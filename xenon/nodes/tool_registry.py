"""Tool registration primitives.

This module is deliberately small and dependency-light.  ``ToolNode`` still
owns the built-in implementations for now, but dispatch metadata lives here so
new tools do not require editing a large ``if``/dispatch block.  The registry
is a migration seam: later tool-family modules can register handlers without
changing the public ``ToolNode.execute`` contract.

Plugin handlers receive ``(node, context)``.  The node is the normalized,
validated invocation and the context is Xenon's ``AgentContext``.  Keeping the
signature explicit makes the extension point usable today while leaving room
for a narrower ``ToolInvocation`` object in a later compatibility release.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


ToolHandler = Callable[[Any, Any], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Metadata and implementation reference for one tool action."""

    name: str
    handler: str | ToolHandler
    description: str = ""
    category: str = "builtin"


class ToolRegistry:
    """Small, deterministic registry for built-in and contributed tools."""

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}

    def register(
        self,
        name: str,
        handler: str | ToolHandler,
        *,
        description: str = "",
        category: str = "plugin",
        replace: bool = False,
    ) -> ToolDefinition:
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("工具名不能为空")
        if not callable(handler) and not isinstance(handler, str):
            raise TypeError("工具处理器必须是可调用对象或方法名")
        if normalized in self._definitions and not replace:
            raise ValueError(f"工具已注册: {normalized}")
        definition = ToolDefinition(
            name=normalized,
            handler=handler,
            description=str(description or ""),
            category=str(category or "plugin"),
        )
        self._definitions[normalized] = definition
        return definition

    def register_method(
        self,
        name: str,
        method_name: str,
        *,
        description: str = "",
        category: str = "builtin",
    ) -> ToolDefinition:
        return self.register(
            name,
            method_name,
            description=description,
            category=category,
        )

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(str(name))

    def contains(self, name: str) -> bool:
        return str(name) in self._definitions

    def names(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    def bind(self, name: str, owner: Any) -> Callable[[Any], dict[str, Any]] | None:
        """Bind a definition to a ToolNode and return a context-only callable."""
        definition = self.get(name)
        if definition is None:
            return None
        if isinstance(definition.handler, str):
            handler = getattr(owner, definition.handler, None)
            if handler is None or not callable(handler):
                raise AttributeError(
                    f"工具 '{definition.name}' 的方法不存在: {definition.handler}"
                )
            return handler

        plugin_handler = definition.handler

        def invoke(context: Any) -> dict[str, Any]:
            return plugin_handler(owner, context)

        return invoke

    def unregister(self, name: str) -> ToolDefinition | None:
        return self._definitions.pop(str(name), None)


# Built-in method names are the single source of truth during the migration.
# ``ToolNode`` imports this mapping for compatibility with its old private
# ``_BUILTIN_ACTION_TYPES`` constant.
BUILTIN_TOOL_METHODS: dict[str, str] = {
    "command": "_exec_command",
    "write_file": "_write_file",
    "read_file": "_read_file",
    "list_files": "_list_files",
    "search_files": "_search_files",
    "git": "_git",
    "web_fetch": "_web_fetch",
    "docs_fetch": "_docs_fetch",
    "edit_file": "_edit_file",
    "create_directory": "_create_directory",
    "batch_write": "_batch_write",
    "batch_edit": "_batch_edit",
    "code_index": "_code_index",
    "ast_analyze": "_ast_analyze",
    "refactor": "_refactor",
    "diff_preview": "_diff_preview",
    "mcp_call": "_mcp_call",
    "github_fetch": "_github_fetch",
    "clone_repo": "_clone_repo",
    "lsp_goto_def": "_lsp_goto_def",
    "lsp_find_refs": "_lsp_find_refs",
    "lsp_hover": "_lsp_hover",
    "lsp_diagnostics": "_lsp_diagnostics",
    "lsp_symbols": "_lsp_symbols",
    "weather": "_weather",
    "datetime": "_datetime",
    "register_tool": "_register_tool",
}

BUILTIN_TOOL_REGISTRY = ToolRegistry()
for _tool_name, _method_name in BUILTIN_TOOL_METHODS.items():
    BUILTIN_TOOL_REGISTRY.register_method(_tool_name, _method_name)


def register_tool_handler(
    name: str,
    handler: ToolHandler,
    *,
    description: str = "",
    category: str = "plugin",
    replace: bool = False,
) -> ToolDefinition:
    """Register a Python handler in the process-wide extension registry."""
    return BUILTIN_TOOL_REGISTRY.register(
        name,
        handler,
        description=description,
        category=category,
        replace=replace,
    )

