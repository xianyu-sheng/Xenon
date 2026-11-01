"""C11: persistent update_plan tool for ReAct long tasks."""

from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.nodes.tool_node import ToolNode


def _run(steps, **kwargs):
    context = AgentContext({})
    node = ToolNode("t", action_type="update_plan", steps=steps, **kwargs)
    result = node.execute(context)
    return result, context


def test_update_plan_stores_plan_and_renders_checklist() -> None:
    result, context = _run(
        [
            {"step": "read module", "status": "completed"},
            {"step": "change API", "status": "in_progress"},
            {"step": "run tests", "status": "pending"},
        ],
        explanation="API changed first",
    )

    assert result["success"] is True
    assert "- [x] read module" in result["content"]
    assert "- [>] change API" in result["content"]
    assert "- [ ] run tests" in result["content"]
    plan = context.get("_agent_plan")
    assert plan["steps"][1] == {"step": "change API", "status": "in_progress"}
    assert plan["explanation"] == "API changed first"


def test_update_plan_accepts_json_string_and_status_aliases() -> None:
    result, context = _run(
        '[{"step": "a", "status": "done"}, {"step": "b", "status": "in-progress"}]'
    )
    assert result["success"] is True
    assert context.get("_agent_plan")["steps"][0]["status"] == "completed"
    assert context.get("_agent_plan")["steps"][1]["status"] == "in_progress"


def test_update_plan_rejects_multiple_in_progress() -> None:
    result, context = _run(
        [
            {"step": "a", "status": "in_progress"},
            {"step": "b", "status": "in_progress"},
        ]
    )
    assert result["success"] is False
    assert "最多只能有一个" in result["error"]
    assert context.get("_agent_plan") is None


def test_update_plan_is_registered() -> None:
    """The tool must stay dispatchable by name.

    The prompt-side schema lived in the deleted engine; the registry is the
    surviving source of truth.  The kernel enforces "plan must be finished"
    through step debt, not through a prompt nudge (#202).
    """

    from xenon.nodes.tool_registry import BUILTIN_TOOL_METHODS

    assert "update_plan" in BUILTIN_TOOL_METHODS
