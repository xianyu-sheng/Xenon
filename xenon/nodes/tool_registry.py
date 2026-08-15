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

## Extension contract (per docs/ARCHITECTURE.md)

    from xenon.nodes.tool_registry import register_tool_handler

    @register_tool_handler(
        "sqlite_query",
        description="对本地 SQLite 数据库执行 SQL 查询",
        params={"db_path": "数据库文件路径", "sql": "SQL 语句"},
        risk="WRITE",   # SELECT 用 INFO，INSERT/UPDATE/DELETE 用 WRITE
    )
    def _handle_sqlite(node, context):
        import sqlite3
        db = getattr(node, "db_path", "")
        sql = getattr(node, "sql", "")
        with sqlite3.connect(db) as conn:
            rows = conn.execute(sql).fetchall()
        return {"action_type": "sqlite_query", "success": True, "content": str(rows)}

三个作用：
1. 向 ``BUILTIN_TOOL_REGISTRY`` 注册分发处理器
2. 向 ``PLUGIN_TOOL_SCHEMAS`` 注入 LLM 可见的名称/描述/参数
3. 向 ``PLUGIN_TOOL_RISK`` 记录风险级别，供 ``classify_tool``/``required_execution_level`` 查询

未声明 ``risk`` 的插件工具默认为 SENSITIVE / level-3（最高风险），
与 MCP 未知工具的默认策略一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal


ToolHandler = Callable[[Any, Any], dict[str, Any]]
ToolRisk = Literal["INFO", "WRITE", "SENSITIVE"]
_VALID_TOOL_RISKS = frozenset({"INFO", "WRITE", "SENSITIVE"})


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Metadata and implementation reference for one tool action."""

    name: str
    handler: str | ToolHandler
    description: str = ""
    category: str = "builtin"
    # 工具参数描述字典，key = 参数名，value = 参数说明；喂给 LLM 的 schema。
    params: dict[str, str] = field(default_factory=dict)
    # 风险级别：INFO=只读无需确认, WRITE=写操作需确认, SENSITIVE=高危需确认。
    # 插件工具默认 SENSITIVE，遵守「未知即从严」原则（与 MCP 未知工具一致）。
    risk: ToolRisk = "SENSITIVE"


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
        params: dict[str, str] | None = None,
        risk: ToolRisk = "SENSITIVE",
        replace: bool = False,
    ) -> ToolDefinition:
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("工具名不能为空")
        if not callable(handler) and not isinstance(handler, str):
            raise TypeError("工具处理器必须是可调用对象或方法名")
        if risk not in _VALID_TOOL_RISKS:
            raise ValueError(
                f"工具风险级别无效: {risk!r}，必须是 INFO、WRITE 或 SENSITIVE"
            )
        if normalized in self._definitions and not replace:
            raise ValueError(f"工具已注册: {normalized}")
        if (
            normalized in self._definitions
            and self._definitions[normalized].category == "builtin"
        ):
            raise ValueError(f"不能覆盖内置工具: {normalized}")
        definition = ToolDefinition(
            name=normalized,
            handler=handler,
            description=str(description or ""),
            category=str(category or "plugin"),
            params=dict(params or {}),
            risk=risk,
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
        params: dict[str, str] | None = None,
        risk: ToolRisk = "SENSITIVE",
    ) -> ToolDefinition:
        return self.register(
            name,
            method_name,
            description=description,
            category=category,
            params=params,
            risk=risk,
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

    def plugin_schemas(self) -> dict[str, dict[str, Any]]:
        """返回所有插件工具（category='plugin'）的 LLM schema 字典。

        格式与 ``react_prompts.BUILTIN_TOOLS`` 一致：
        ``{name: {name, description, params}}``。
        引擎在初始化时将这些 schema 合并入 ``self.tools``，让 LLM 知道插件工具存在。
        """
        return {
            d.name: {"name": d.name, "description": d.description, "params": d.params}
            for d in self._definitions.values()
            if d.category == "plugin"
        }


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
    "spawn_agent": "_spawn_agent",  # 引擎层拦截，注册表仅声明 risk 分类
}

BUILTIN_TOOL_REGISTRY = ToolRegistry()
for _tool_name, _method_name in BUILTIN_TOOL_METHODS.items():
    # 内置工具的风险级别在此声明。写操作工具用 WRITE，高危用 SENSITIVE，
    # 只读用 INFO。此前 tool_executor.py 里有两份硬编码集合；现在注册表是
    # 唯一来源，tool_executor 只需查 definition.risk。
    _risk: ToolRisk = "SENSITIVE"
    if _tool_name in {
        "read_file", "list_files", "search_files", "code_index",
        "ast_analyze", "diff_preview", "web_fetch", "docs_fetch",
        "github_fetch", "weather", "datetime", "lsp_hover",
        "lsp_goto_def", "lsp_find_refs", "lsp_diagnostics", "lsp_symbols",
    }:
        _risk = "INFO"
    elif _tool_name in {
        "write_file", "edit_file", "create_directory", "batch_write",
        "batch_edit", "refactor", "git", "clone_repo",
    }:
        _risk = "WRITE"
    # command / mcp_call / register_tool 保持 SENSITIVE（默认值）
    BUILTIN_TOOL_REGISTRY.register_method(_tool_name, _method_name, risk=_risk)


def register_tool_handler(
    name: str,
    handler: ToolHandler | None = None,
    *,
    description: str = "",
    params: dict[str, str] | None = None,
    risk: ToolRisk = "SENSITIVE",
    category: str = "plugin",
    replace: bool = False,
) -> Any:
    """Register a Python handler in the process-wide extension registry.

    可作为普通函数或装饰器使用::

        # 函数式
        register_tool_handler("my_tool", my_handler, description="...",
                              params={"key": "说明"}, risk="INFO")

        # 装饰器
        @register_tool_handler("my_tool", description="...",
                               params={"key": "说明"}, risk="WRITE")
        def my_handler(node, context):
            ...

    注册后，工具会：
    1. 被 BUILTIN_TOOL_REGISTRY 分发（``ToolNode`` 能执行它）
    2. 出现在 ``BUILTIN_TOOL_REGISTRY.plugin_schemas()`` 里（引擎启动时合并进
       ``self.tools``，LLM 能看见它）
    3. 被 ``classify_tool`` / ``required_execution_level`` 正确分类
       （``risk`` 默认 SENSITIVE，不会静默绕过权限确认）
    """
    if handler is None:
        # 装饰器用法：register_tool_handler("name", description=...) 返回 decorator
        def decorator(fn: ToolHandler) -> ToolHandler:
            BUILTIN_TOOL_REGISTRY.register(
                name, fn,
                description=description, params=params, risk=risk,
                category=category, replace=replace,
            )
            return fn
        return decorator

    # 函数式用法：register_tool_handler("name", handler, ...)
    return BUILTIN_TOOL_REGISTRY.register(
        name, handler,
        description=description, params=params or {}, risk=risk,
        category=category, replace=replace,
    )
