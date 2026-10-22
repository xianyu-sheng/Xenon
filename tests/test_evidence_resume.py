"""Durable evidence must survive auto-save and be replayable after resume."""

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
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL


def _repl() -> REPL:
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    registry.assign_role("planner", ["test"])
    return REPL(registry=registry, streaming=False)


def _observation(
    ledger: EvidenceLedger,
    task_id: str,
    tool: str,
    *,
    params: dict,
    summary: str,
    metadata: dict,
) -> None:
    ledger.append(
        LifecyclePhase.POST_TOOL,
        EventKind.TOOL_OBSERVATION,
        EvidenceSource.TOOL,
        {
            "task_id": task_id,
            "tool": tool,
            "params": params,
            "success": True,
            "summary": summary,
            "metadata": metadata,
        },
    )


def test_auto_save_persists_ledger_and_projection_replays_after_load(
    tmp_path, monkeypatch
) -> None:
    from xenon.repl import session as session_mod

    monkeypatch.setattr(session_mod, "SESSIONS_DIR", tmp_path)
    state = TaskState(
        objective="implement",
        intent="write_code",
        execution_level=3,
        required=["verify", "commit"],
    )
    ledger = EvidenceLedger("resume-ledger")
    ledger.append(
        LifecyclePhase.TASK,
        EventKind.TASK_FACT,
        EvidenceSource.ENGINE,
        {"engine": "fake", "task_id": state.task_id},
    )
    ledger.append(
        LifecyclePhase.PLANNING,
        EventKind.PLAN,
        EvidenceSource.ENGINE,
        {
            "task_id": state.task_id,
            "steps": [{"id": 1, "task": "implement", "tool": "write_file"}],
        },
    )
    ledger.append(
        LifecyclePhase.EXECUTION,
        EventKind.PLAN_STEP_FINISHED,
        EvidenceSource.ENGINE,
        {"task_id": state.task_id, "step_id": 1, "success": True, "status": "ok"},
    )
    _observation(
        ledger,
        state.task_id,
        "write_file",
        params={"file_path": "src/app.py"},
        summary="written",
        metadata={"result_id": "r-write", "changed_paths": ["src/app.py"]},
    )
    _observation(
        ledger,
        state.task_id,
        "command",
        params={"command": "pytest -q"},
        summary="2 passed",
        metadata={"result_id": "r-test", "exit_code": 0},
    )
    _observation(
        ledger,
        state.task_id,
        "git",
        params={"git_command": "commit -m done"},
        summary="[main abc1234] done",
        metadata={"result_id": "r-commit", "committed_paths": ["src/app.py"]},
    )

    repl = _repl()
    repl.agent_context = AgentContext(
        {"_task_state": state.to_dict(), "_evidence_ledger": ledger}
    )
    repl._auto_save_session(cleanup=False)

    data = session_mod.load_session("_auto")
    assert data["extra"]["evidence"]["session_id"] == "resume-ledger"
    restored_ledger = EvidenceLedger.from_snapshot(data["extra"]["evidence"])
    restored_context = AgentContext(initial=data["context"])
    restored_context.bind_evidence(restored_ledger)

    restored_state = TaskState.from_context(restored_context)
    assert restored_state is not None
    TaskStateProjection(restored_state, restored_context).attach(
        restored_ledger, replay=True
    ).close()

    assert restored_state.steps[0]["status"] == "completed"
    assert restored_state.artifacts == ["src/app.py"]
    assert restored_state.tests
    assert restored_state.commit == "abc1234"
    assert restored_state.missing_delivery() == []
