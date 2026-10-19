"""Durable TaskState: plan, artifacts and delivery gates across turns."""

from __future__ import annotations

from types import SimpleNamespace

from xenon.engine.task_state import (
    TaskState,
    required_delivery_for,
    task_state_block,
)


def test_issue_delivery_requires_verify_commit_and_pr() -> None:
    required = required_delivery_for(
        "实现 issue #22，完成后创建 PR",
        level=3,
        write_patterns=["task_delivery"],
    )
    assert required == ["commit", "pr", "verify"]


def test_plain_write_only_requires_verification() -> None:
    assert required_delivery_for("修改 README", level=2) == ["verify"]
    assert required_delivery_for("查看 README", level=1) == []
    # Doc/report writes must not trigger code verification (C15).
    assert required_delivery_for("写报告", level=2, intent="write_report") == []
    assert required_delivery_for("写文档", level=2, intent="write_doc") == []


def test_state_tracks_writes_tests_commit_and_pr() -> None:
    state = TaskState(
        objective="implement #22",
        intent="write_code",
        execution_level=3,
        required=["verify", "commit", "pr"],
    )
    state.record_tool(
        "write_file",
        {"file_path": "smartbench/reporting/html.py"},
        success=True,
    )
    state.record_tool(
        "command",
        {"command": "cd repo; python -m pytest tests/test_html.py -q"},
        success=True,
        result="10 passed",
    )
    state.record_tool(
        "command",
        {"command": "git commit -m 'feat: report'"},
        success=True,
        result="[main abc1234] feat: report",
    )
    state.record_tool(
        "create_pull_request",
        {"repo": "owner/repo"},
        success=True,
        result="已创建 PR #51: https://github.com/owner/repo/pull/51",
    )

    assert "smartbench/reporting/html.py" in state.artifacts
    assert state.tests
    assert state.commit == "abc1234"
    assert state.pr_url.endswith("/pull/51")
    assert state.missing_delivery() == []


def test_state_can_be_restored_from_context_and_renders_pending() -> None:
    state = TaskState(
        objective="finish issue",
        execution_level=3,
        steps=[{"step": "run tests", "status": "pending"}],
        required=["verify", "commit"],
    )
    ctx = SimpleNamespace(get=lambda key: state.to_dict() if key == "_task_state" else None)

    restored = TaskState.from_context(ctx)
    assert restored is not None
    assert restored.objective == "finish issue"
    assert set(restored.missing_delivery()) == {"commit", "verify"}
    block = task_state_block(ctx)
    assert "任务状态" in block
    assert "[ ] run tests" in block
    assert "运行相关测试" in block


def test_update_plan_and_failed_calls_do_not_fake_progress() -> None:
    state = TaskState(objective="x", execution_level=2, required=["verify"])
    state.record_tool(
        "update_plan",
        {"steps": [{"step": "a", "status": "completed"}, {"step": "b", "status": "pending"}]},
        success=True,
    )
    state.record_tool("command", {"command": "pytest -q"}, success=False, error="failed")
    state.record_tool(
        "write_file", {"file_path": "x.py"}, success=False, error="denied"
    )

    assert state.incomplete_steps() == [{"step": "b", "status": "pending"}]
    assert state.tests == []
    assert state.artifacts == []


def test_calls_are_folded_from_tracker_objects() -> None:
    state = TaskState(objective="x", execution_level=2, required=["verify"])
    calls = [
        SimpleNamespace(
            tool_name="command",
            params={"command": "pytest -q"},
            success=True,
            result_summary="2 passed",
            error=None,
        )
    ]
    state.update_from_calls(calls)
    assert state.tests
    assert state.missing_delivery() == []


def test_all_engine_families_share_the_same_delivery_exit() -> None:
    from xenon.engine.base import BaseEngine
    from xenon.engine.combined_engines import SteeringMixin

    assert hasattr(BaseEngine, "run_task")
    assert hasattr(SteeringMixin, "run_task")
