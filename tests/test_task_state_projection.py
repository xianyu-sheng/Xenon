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
    projection(event)

    assert state.artifacts == ["src/app.py"]
    assert state.artifacts_revision == 1
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
