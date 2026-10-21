"""Plan-Execute must replan unfinished work after a failed step."""

from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.engine.plan_dag_executor import StepOutcome
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.tool_tracker import ToolExecutionTracker


def test_failed_step_triggers_bounded_replan():
    engine = PlanExecuteEngine(
        ["fake/model"],
        max_steps=0,
        max_replans=2,
        tool_remediation_attempts=0,
    )
    ctx = AgentContext({})
    tracker = ToolExecutionTracker()
    steps = [
        {"id": 1, "task": "write the file", "tool": "edit_file", "params": {}},
    ]
    executed: list[int] = []

    def fail_first(step_id, *args, **kwargs):
        executed.append(step_id)
        return StepOutcome("still missing", False, "edit failed")

    def replan(*args, **kwargs):
        return [{"id": 2, "task": "repair the file", "tool": None, "params": {}}]

    engine._execute_tool_step = fail_first  # type: ignore[method-assign]
    engine._replan_remaining = replan  # type: ignore[method-assign]
    engine._execute_step_with_llm = lambda *args, **kwargs: "repaired"  # type: ignore[method-assign]

    results = engine._run_serial(steps, "implement the fix", ctx, tracker, 1)

    assert executed == [1]
    assert engine._replans_used == 1
    assert [item["status"] for item in results] == ["failed", "ok"]
    assert results[1]["task"] == "repair the file"
