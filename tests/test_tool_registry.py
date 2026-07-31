from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.nodes.tool_node import ToolNode
from xenon.nodes.tool_registry import (
    BUILTIN_TOOL_REGISTRY,
    ToolRegistry,
    register_tool_handler,
)


def test_builtin_registry_is_the_dispatch_source() -> None:
    assert BUILTIN_TOOL_REGISTRY.contains("command")
    assert BUILTIN_TOOL_REGISTRY.get("command").handler == "_exec_command"
    assert "docs_fetch" in BUILTIN_TOOL_REGISTRY.names()


def test_plugin_handler_can_execute_without_editing_tool_node() -> None:
    name = "test_registry_plugin"

    def handler(node, context):
        return {
            "action_type": node.action_type,
            "success": True,
            "content": str(context.get("value", "missing")),
        }

    register_tool_handler(name, handler, description="registry test")
    try:
        result = ToolNode("plugin", action_type=name).execute(
            AgentContext(initial={"value": "ok"})
        )
        assert result["success"] is True
        assert result["content"] == "ok"
    finally:
        BUILTIN_TOOL_REGISTRY.unregister(name)


def test_registry_rejects_accidental_duplicate_registration() -> None:
    registry = ToolRegistry()
    registry.register_method("demo", "_demo")
    try:
        registry.register_method("demo", "_other")
    except ValueError as exc:
        assert "工具已注册" in str(exc)
    else:  # pragma: no cover - assertion keeps the contract explicit
        raise AssertionError("duplicate tool registration must fail")

