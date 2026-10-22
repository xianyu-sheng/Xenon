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


def test_failed_dag_wave_replans_before_skip_cascade():
    engine = PlanExecuteEngine(
        ["fake/model"],
        max_steps=0,
        max_replans=2,
        tool_remediation_attempts=0,
    )
    ctx = AgentContext({})
    tracker = ToolExecutionTracker()
    steps = [
        {"id": 1, "task": "write", "tool": "write_file", "params": {}, "depends_on": []},
        {"id": 2, "task": "verify", "tool": None, "params": {}, "depends_on": [1]},
    ]

    engine._execute_tool_step = lambda *args, **kwargs: StepOutcome(
        "write failed", False, "permission denied"
    )
    engine._replan_remaining = lambda *args, **kwargs: [
        {"id": 3, "task": "repair", "tool": None, "params": {}, "depends_on": []}
    ]
    engine._execute_step_with_llm = lambda *args, **kwargs: "repaired"

    results = engine._run_dag(
        steps,
        "implement the fix",
        ctx,
        tracker,
        2,
        replan_on_failure=True,
    )

    assert engine._replans_used == 1
    assert [item["step_id"] for item in results] == [3]
    assert [item["status"] for item in results] == ["ok"]
    assert ctx.get("step_3_status") == "ok"
    # The old dependent step was never turned into a skipped row; its plan
    # was replaced before the next wave.
    assert ctx.get("step_2_status") is None


def test_llm_step_that_requires_write_cannot_fake_completion():
    engine = PlanExecuteEngine(["fake/model"], max_steps=0, max_replans=0)
    tracker = ToolExecutionTracker()
    steps = [
        {
            "id": 1,
            "task": "实际插入覆盖摘要行到目标文件",
            "tool": None,
            "params": {},
            "depends_on": [],
        }
    ]
    engine._execute_step_with_llm = lambda *args, **kwargs: "done without writing"

    results = engine._run_serial(steps, "implement", AgentContext(), tracker, 1)

    assert results[0]["status"] == "failed"
    assert "写操作" in results[0]["error"]


def test_llm_step_with_real_write_passes():
    engine = PlanExecuteEngine(["fake/model"], max_steps=0, max_replans=0)
    tracker = ToolExecutionTracker()
    steps = [
        {
            "id": 1,
            "task": "实际插入覆盖摘要行到目标文件",
            "tool": None,
            "params": {},
            "depends_on": [],
        }
    ]

    def write_then_finish(*args, **kwargs):
        tracker.record(
            "write_file",
            {"file_path": "src/app.py"},
            True,
            "written",
        )
        return "done"

    engine._execute_step_with_llm = write_then_finish
    results = engine._run_serial(steps, "implement", AgentContext(), tracker, 1)

    assert results[0]["status"] == "ok"


def test_failed_step_is_repaired_in_place_without_full_replan():
    engine = PlanExecuteEngine(["fake/model"], max_steps=0, max_replans=2)
    ctx = AgentContext({})
    tracker = ToolExecutionTracker()
    steps = [
        {
            "id": 1,
            "task": "write the source file",
            "tool": None,
            "params": {},
            "kind": "edit",
            "requires_write": True,
        }
    ]
    calls = {"n": 0}
    replans: list[object] = []

    def execute(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "执行失败: file not found"
        tracker.record("write_file", {"file_path": "src/app.py"}, True, "written")
        return "fixed"

    engine._execute_step_with_llm = execute  # type: ignore[method-assign]
    engine._replan_remaining = lambda *args, **kwargs: replans.append(1) or []  # type: ignore[method-assign]

    results = engine._run_serial(steps, "implement", ctx, tracker, 1)

    assert calls["n"] == 2
    assert results[0]["status"] == "ok"
    assert engine._replans_used == 0
    assert replans == []


def test_replan_that_drops_write_step_is_rejected():
    engine = PlanExecuteEngine(["fake/model"], max_steps=0, max_replans=2)
    engine._plan = lambda *args, **kwargs: {  # type: ignore[method-assign]
        "steps": [
            {
                "id": 9,
                "task": "read more files",
                "tool": "read_file",
                "params": {},
            }
        ]
    }

    new_steps = engine._replan_remaining(
        "修复代码并运行测试",
        AgentContext({}),
        [{"text": "step failed"}],
        set(),
        failure=True,
        tracker=ToolExecutionTracker(),
    )

    assert new_steps == []
