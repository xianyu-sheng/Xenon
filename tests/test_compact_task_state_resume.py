"""Compact must not erase durable TaskState; auto-save/resume must restore it."""

from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.engine.task_state import TaskState
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL


def _repl() -> REPL:
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    registry.assign_role("planner", ["test"])
    return REPL(registry=registry, streaming=False)


def test_compact_preserves_task_state_through_auto_save_and_resume(
    tmp_path, monkeypatch
) -> None:
    from xenon.repl import session as session_mod

    monkeypatch.setattr(session_mod, "SESSIONS_DIR", tmp_path)
    repl = _repl()
    state = TaskState(
        objective="实现 #26 的覆盖统计切片",
        intent="write_code",
        execution_level=3,
        steps=[
            {"step": "读取 unified.py", "status": "completed"},
            {"step": "修改 print_diagnosis_result", "status": "pending"},
        ],
        artifacts=["smartbench/cli/unified.py"],
        tests=["pytest tests/test_cli_coverage_summary.py -q"],
        commit="abc1234",
        required=["verify", "commit"],
    )
    repl.agent_context.set("_task_state", state.to_dict())

    for i in range(8):
        repl.ctx_mgr.add_message("user", f"turn {i}")
        repl.ctx_mgr.add_message("assistant", f"answer {i}")

    before = repl.agent_context.get("_task_state").copy()
    repl.ctx_mgr.compact(summary="compacted history keeps the task state")

    # /compact only rewrites conversation history; execution state must survive.
    assert repl.agent_context.get("_task_state") == before

    repl._auto_save_session(cleanup=False)
    data = session_mod.load_session("_auto")
    restored_ctx = AgentContext(initial=data["context"])
    restored = TaskState.from_context(restored_ctx)

    assert restored is not None
    assert restored.objective == state.objective
    assert restored.steps == state.steps
    assert restored.artifacts == state.artifacts
    assert restored.tests == state.tests
    assert restored.commit == state.commit
    assert restored.required == state.required
    assert "实现 #26" in restored.delivery_prompt()


def test_compact_does_not_turn_an_incomplete_task_into_completed(tmp_path, monkeypatch) -> None:
    from xenon.repl import session as session_mod

    monkeypatch.setattr(session_mod, "SESSIONS_DIR", tmp_path)
    repl = _repl()
    state = TaskState(
        objective="implement #26",
        intent="write_code",
        execution_level=3,
        artifacts=["tests/test_cli_coverage_summary.py"],
        commit="90de008",
        required=["verify", "commit"],
    )
    repl.agent_context.set("_task_state", state.to_dict())
    for i in range(6):
        repl.ctx_mgr.add_message("user", f"u{i}")
        repl.ctx_mgr.add_message("assistant", f"a{i}")

    repl.ctx_mgr.compact(summary="summary")
    repl._auto_save_session(cleanup=False)
    restored = TaskState.from_context(
        AgentContext(initial=session_mod.load_session("_auto")["context"])
    )

    assert restored is not None
    assert "changes" in restored.missing_delivery()
    assert restored.maybe_complete() is False
