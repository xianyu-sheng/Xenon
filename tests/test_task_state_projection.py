"""TaskState must be a replayable projection of durable tool observations."""

from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.engine.evidence_runtime import (
    EventKind,
    EvidenceLedger,
    EvidenceSource,
    LifecyclePhase,
)
from xenon.engine.task_state import TaskState
from xenon.engine.task_state_projection import TaskStateProjection


def _state() -> TaskState:
    return TaskState(
        objective="implement",
        intent="write_code",
        execution_level=3,
        required=["verify", "commit"],
    )


def _append_observation(
    ledger: EvidenceLedger,
    tool: str,
    *,
    params: dict,
    success: bool,
    summary: str,
    task_id: str = "",
    metadata: dict | None = None,
) -> None:
    payload = {
        "tool": tool,
        "params": params,
        "success": success,
        "summary": summary,
        "metadata": metadata or {},
    }
    if task_id:
        payload["task_id"] = task_id
    ledger.append(
        LifecyclePhase.POST_TOOL,
        EventKind.TOOL_OBSERVATION,
        EvidenceSource.TOOL,
        payload,
    )


def test_projection_applies_writes_tests_and_commit() -> None:
    state = _state()
    context = AgentContext({"_task_state": state.to_dict()})
    ledger = EvidenceLedger("run-projection")
    projection = TaskStateProjection(state, context).attach(ledger)

    _append_observation(
        ledger,
        "write_file",
        params={"file_path": "src/app.py"},
        success=True,
        summary="written",
        task_id=state.task_id,
        metadata={"result_id": "r-write", "changed_paths": ["src/app.py"]},
    )
    _append_observation(
        ledger,
        "command",
        params={"command": "pytest -q"},
        success=True,
        summary="2 passed",
        task_id=state.task_id,
        metadata={"result_id": "r-test", "exit_code": 0},
    )
    _append_observation(
        ledger,
        "git",
        params={"git_command": "commit -m done"},
        success=True,
        summary="[main abc1234] done",
        task_id=state.task_id,
        metadata={
            "result_id": "r-commit",
            "committed_paths": ["src/app.py"],
        },
    )

    assert state.artifacts == ["src/app.py"]
    assert state.tests
    assert state.commit == "abc1234"
    assert state.missing_delivery() == []
    assert context.get("_task_state")["commit"] == "abc1234"
    projection.close()


def test_projection_is_idempotent_by_result_id() -> None:
    state = _state()
    ledger = EvidenceLedger("run-idempotent")
    projection = TaskStateProjection(state).attach(ledger)
    _append_observation(
        ledger,
        "write_file",
        params={"file_path": "src/app.py"},
        success=True,
        summary="written",
        task_id=state.task_id,
        metadata={"result_id": "same-call", "changed_paths": ["src/app.py"]},
    )

    event = ledger.events[-1]
    state.processed_tool_ids.clear()
    projection(event)

    assert state.artifacts == ["src/app.py"]
    assert state.artifacts_revision == 1
    assert state.evidence_sequence == event.sequence
    projection.close()


def test_projection_replays_from_task_boundary_and_ignores_other_tasks() -> None:
    state = _state()
    ledger = EvidenceLedger("run-replay")
    _append_observation(
        ledger,
        "write_file",
        params={"file_path": "old.py"},
        success=True,
        summary="old",
        task_id="other-task",
        metadata={"result_id": "old"},
    )
    _append_observation(
        ledger,
        "write_file",
        params={"file_path": "src/app.py"},
        success=True,
        summary="new",
        task_id=state.task_id,
        metadata={"result_id": "new"},
    )

    TaskStateProjection(state).attach(ledger, replay=True).close()

    assert state.artifacts == ["src/app.py"]


def test_projection_close_stops_updates() -> None:
    state = _state()
    ledger = EvidenceLedger("run-close")
    projection = TaskStateProjection(state).attach(ledger)
    projection.close()

    _append_observation(
        ledger,
        "write_file",
        params={"file_path": "src/app.py"},
        success=True,
        summary="written",
        task_id=state.task_id,
        metadata={"result_id": "after-close"},
    )

    assert state.artifacts == []


class _ProjectedEngine:
    def __init__(self) -> None:
        from xenon.engine.callbacks import EngineCallback
        from xenon.engine.tool_tracker import ToolExecutionTracker

        self.callback = EngineCallback()
        self._last_tracker = ToolExecutionTracker()
        self._last_termination_reason = None

    def run(self, user_input, context=None, ctx_mgr=None):
        return "done"


def test_delivery_runtime_replays_ledger_into_task_state() -> None:
    from xenon.engine.result import TerminationReason
    from xenon.engine.task_runtime import run_with_delivery

    state = _state()
    ledger = EvidenceLedger("run-runtime-projection")
    ledger.append(
        LifecyclePhase.TASK,
        EventKind.TASK_FACT,
        EvidenceSource.ENGINE,
        {"engine": "fake", "task_id": state.task_id},
    )
    _append_observation(
        ledger,
        "write_file",
        params={"file_path": "src/app.py"},
        success=True,
        summary="written",
        task_id=state.task_id,
        metadata={"result_id": "r-write", "changed_paths": ["src/app.py"]},
    )
    _append_observation(
        ledger,
        "command",
        params={"command": "pytest -q"},
        success=True,
        summary="2 passed",
        task_id=state.task_id,
        metadata={"result_id": "r-test", "exit_code": 0},
    )
    _append_observation(
        ledger,
        "git",
        params={"git_command": "commit -m done"},
        success=True,
        summary="[main abc1234] done",
        task_id=state.task_id,
        metadata={"result_id": "r-commit", "committed_paths": ["src/app.py"]},
    )
    context = AgentContext(
        {
            "_task_state": state.to_dict(),
            "_evidence_ledger": ledger,
        }
    )

    result = run_with_delivery(_ProjectedEngine(), "finish", context)

    assert result == "done"
    assert result.reason is TerminationReason.FINAL
    assert context.get("_task_state")["status"] == "completed"
    assert context.get("_task_state")["commit"] == "abc1234"


def test_projection_applies_plan_and_step_events() -> None:
    state = _state()
    ledger = EvidenceLedger("run-plan-projection")
    ledger.append(
        LifecyclePhase.TASK,
        EventKind.TASK_FACT,
        EvidenceSource.ENGINE,
        {"engine": "plan", "task_id": state.task_id},
    )
    ledger.append(
        LifecyclePhase.PLANNING,
        EventKind.PLAN,
        EvidenceSource.ENGINE,
        {
            "task_id": state.task_id,
            "steps": [
                {"id": 10, "task": "read", "tool": "read_file"},
                {"id": 20, "task": "write", "tool": "write_file"},
            ],
        },
    )
    ledger.append(
        LifecyclePhase.EXECUTION,
        EventKind.PLAN_STEP_STARTED,
        EvidenceSource.ENGINE,
        {"task_id": state.task_id, "step_id": 10, "task": "read"},
    )
    ledger.append(
        LifecyclePhase.EXECUTION,
        EventKind.PLAN_STEP_FINISHED,
        EvidenceSource.ENGINE,
        {
            "task_id": state.task_id,
            "step_id": 10,
            "success": True,
            "status": "ok",
        },
    )
    ledger.append(
        LifecyclePhase.EXECUTION,
        EventKind.PLAN_STEP_FINISHED,
        EvidenceSource.ENGINE,
        {
            "task_id": state.task_id,
            "step_id": 20,
            "success": False,
            "status": "failed",
        },
    )

    TaskStateProjection(state).attach(ledger, replay=True).close()

    assert [step["id"] for step in state.steps] == ["10", "20"]
    assert [step["status"] for step in state.steps] == ["completed", "failed"]
    assert state.maybe_complete() is False


def test_projection_notifies_only_plan_or_step_changes() -> None:
    state = _state()
    ledger = EvidenceLedger("run-plan-change")
    seen: list[str] = []
    projection = TaskStateProjection(
        state, on_change=lambda snapshot: seen.append(snapshot.plan_summary())
    ).attach(ledger)

    _append_observation(
        ledger,
        "write_file",
        params={"file_path": "src/app.py"},
        success=True,
        summary="written",
        task_id=state.task_id,
        metadata={"result_id": "r-write"},
    )
    assert seen == []

    ledger.append(
        LifecyclePhase.PLANNING,
        EventKind.PLAN,
        EvidenceSource.ENGINE,
        {
            "task_id": state.task_id,
            "version": 1,
            "steps": [{"id": 1, "task": "write", "status": "pending"}],
        },
    )
    assert seen
    assert "Plan v1" in seen[-1]
    projection.close()


def test_delivery_runtime_publishes_live_plan_state_on_engine_bus() -> None:
    from xenon.engine.event_bus import EventBus
    from xenon.engine.result import TerminationReason
    from xenon.engine.task_runtime import run_with_delivery

    class _EngineWithPlan(_ProjectedEngine):
        def __init__(self) -> None:
            super().__init__()
            self.event_bus = EventBus()

        def run(self, user_input, context=None, ctx_mgr=None):
            ledger = context.get("_evidence_ledger")
            ledger.append(
                LifecyclePhase.PLANNING,
                EventKind.PLAN,
                EvidenceSource.ENGINE,
                {
                    "task_id": context.get("_task_state")["task_id"],
                    "version": 1,
                    "steps": [{"id": 1, "task": "write", "status": "pending"}],
                },
            )
            return "done"

    state = _state()
    ledger = EvidenceLedger("run-live-plan")
    ledger.append(
        LifecyclePhase.TASK,
        EventKind.TASK_FACT,
        EvidenceSource.ENGINE,
        {"task_id": state.task_id},
    )
    context = AgentContext(
        {"_task_state": state.to_dict(), "_evidence_ledger": ledger}
    )
    engine = _EngineWithPlan()
    seen: list[str] = []
    engine.event_bus.subscribe(
        "plan_state", lambda summary, snapshot: seen.append(summary)
    )

    result = run_with_delivery(engine, "finish", context, max_delivery_retries=0)

    assert isinstance(result, str)
    assert seen and "Plan v1" in seen[0]
    assert context.get("_task_state")["steps"][0]["status"] == "pending"
    assert result.reason == TerminationReason.DELIVERY_INCOMPLETE


def test_delivery_runtime_replays_ledger_created_during_run() -> None:
    from xenon.engine.task_runtime import run_with_delivery

    class _EngineCreatesLedger(_ProjectedEngine):
        def run(self, user_input, context=None, ctx_mgr=None):
            ledger = EvidenceLedger("created-during-run")
            state = TaskState.from_context(context)
            ledger.append(
                LifecyclePhase.TASK,
                EventKind.TASK_FACT,
                EvidenceSource.ENGINE,
                {"task_id": state.task_id},
            )
            ledger.append(
                LifecyclePhase.PLANNING,
                EventKind.PLAN,
                EvidenceSource.ENGINE,
                {
                    "task_id": state.task_id,
                    "version": 1,
                    "steps": [{"id": 1, "task": "write", "status": "pending"}],
                },
            )
            context.set("_evidence_ledger", ledger)
            return "done"

    state = _state()
    context = AgentContext({"_task_state": state.to_dict()})

    run_with_delivery(_EngineCreatesLedger(), "finish", context, max_delivery_retries=0)

    projected = TaskState.from_context(context)
    assert projected is not None
    assert [step["step"] for step in projected.steps] == ["write"]


def test_new_ledger_session_resets_the_sequence_cursor() -> None:
    state = _state()
    state.evidence_sequence = 99
    state.evidence_session_id = "old-session"
    ledger = EvidenceLedger("new-session")
    projection = TaskStateProjection(state).attach(ledger)

    _append_observation(
        ledger,
        "write_file",
        params={"file_path": "src/app.py"},
        success=True,
        summary="written",
        task_id=state.task_id,
        metadata={"result_id": "new-stream"},
    )

    assert state.artifacts == ["src/app.py"]
    assert state.evidence_sequence == 1
    assert state.evidence_session_id == "new-session"
    projection.close()
