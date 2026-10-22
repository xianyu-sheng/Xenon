"""Plan-Execute plans must be reflected in durable TaskState."""

from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.task_state import TaskState


def _ctx() -> AgentContext:
    state = TaskState(
        objective="implement the plan",
        intent="write_code",
        execution_level=3,
        required=["verify", "commit"],
    )
    return AgentContext({"_task_state": state.to_dict()})


def test_plan_and_failed_result_keep_task_incomplete():
    ctx = _ctx()
    steps = [
        {"id": 1, "task": "read source", "tool": "read_file", "params": {}},
        {"id": 2, "task": "write source", "tool": "edit_file", "params": {}},
    ]

    PlanExecuteEngine._sync_task_plan(ctx, steps)
    state = TaskState.from_context(ctx)
    assert state is not None
    assert [step["status"] for step in state.steps] == ["pending", "pending"]

    PlanExecuteEngine._sync_task_results(
        ctx,
        [{"step_id": 1, "status": "ok"}, {"step_id": 2, "status": "failed"}],
    )
    state = TaskState.from_context(ctx)
    assert state is not None
    assert [step["status"] for step in state.steps] == ["completed", "failed"]
    assert state.maybe_complete() is False
    assert state.status == "active"


def test_results_sync_by_real_step_id_not_list_position():
    ctx = _ctx()
    steps = [
        {"id": 10, "task": "read", "tool": "read_file", "params": {}},
        {"id": 20, "task": "write", "tool": "edit_file", "params": {}},
    ]

    PlanExecuteEngine._sync_task_plan(ctx, steps)
    PlanExecuteEngine._sync_task_results(
        ctx,
        [
            {"step_id": 10, "status": "ok"},
            {"step_id": 20, "status": "failed"},
        ],
    )

    state = TaskState.from_context(ctx)
    assert state is not None
    assert [step["id"] for step in state.steps] == ["10", "20"]
    assert [step["status"] for step in state.steps] == ["completed", "failed"]
    assert state.maybe_complete() is False


def test_plan_execute_emits_plan_and_step_events_for_projection():
    from xenon.engine.evidence_runtime import (
        EventKind,
        EvidenceLedger,
        EvidenceSource,
        LifecyclePhase,
    )
    from xenon.engine.task_state_projection import TaskStateProjection
    from xenon.engine.tool_tracker import ToolExecutionTracker

    state = TaskState(
        objective="implement the plan",
        intent="write_code",
        execution_level=3,
        required=["verify", "commit"],
    )
    ledger = EvidenceLedger("run-plan-events")
    ledger.append(
        LifecyclePhase.TASK,
        EventKind.TASK_FACT,
        EvidenceSource.ENGINE,
        {"engine": "plan", "task_id": state.task_id},
    )
    ctx = AgentContext(
        {"_task_state": state.to_dict(), "_evidence_ledger": ledger}
    )
    engine = PlanExecuteEngine(["model"], max_steps=0)
    engine._execute_step_with_llm = lambda *args, **kwargs: "ok"
    steps = [
        {"id": 10, "task": "read", "tool": None, "params": {}, "depends_on": []},
        {"id": 20, "task": "write", "tool": None, "params": {}, "depends_on": []},
    ]

    engine._record_plan_event(ctx, steps)
    engine._run_serial(steps, "task", ctx, ToolExecutionTracker(), 2)

    kinds = [event.kind for event in ledger.events]
    assert EventKind.PLAN.value in kinds
    assert kinds.count(EventKind.PLAN_STEP_STARTED.value) == 2
    assert kinds.count(EventKind.PLAN_STEP_FINISHED.value) == 2

    TaskStateProjection(state).attach(ledger, replay=True).close()
    assert [step["status"] for step in state.steps] == ["completed", "completed"]
