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
        artifacts=["src/task.py"],
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

    assert [
        (step["step"], step["status"])
        for step in state.incomplete_steps()
    ] == [("b", "pending")]
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


def test_commit_gate_ignores_old_hashes_from_log_output() -> None:
    state = TaskState(objective="x", execution_level=3, required=["commit"])
    state.record_tool(
        "command",
        {"command": "git log --oneline -3"},
        success=True,
        result="02d9eb9 Merge pull request #51\n005ba58 fix: old",
    )
    assert state.commit == ""

    state.record_tool(
        "command",
        {"command": "git commit -m 'new work'"},
        success=True,
        result="[main 88ffd05] new work",
    )
    assert state.commit == "88ffd05"


def test_test_only_commit_does_not_complete_code_task() -> None:
    state = TaskState(
        objective="implement the fix",
        intent="write_code",
        execution_level=3,
        artifacts=["tests/test_fix.py"],
        tests=["pytest -q"],
        commit="abc1234",
        required=["verify", "commit"],
    )

    assert "changes" in state.missing_delivery()
    assert state.maybe_complete() is False
    assert state.status == "active"


def test_git_commit_gate_reads_git_command_and_requires_a_hash() -> None:
    state = TaskState(objective="x", execution_level=3, required=["commit"])
    state.record_tool(
        "git",
        {"action": "commit -m \"fake\""},
        success=True,
        result="",
    )
    assert state.commit == ""

    state.record_tool(
        "git",
        {"git_command": "commit -m \"real\""},
        success=True,
        result="[main 88ffd05] real",
    )
    assert state.commit == "88ffd05"


def test_commit_hash_uses_commit_summary_not_trailing_log() -> None:
    state = TaskState(objective="x", execution_level=3, required=["commit"])
    state.record_tool(
        "command",
        {"command": "git commit -m x; git log --oneline -3"},
        success=True,
        result="[main abc1234] x\nold 412543c docs",
    )
    assert state.commit == "abc1234"


def test_failed_test_output_does_not_satisfy_verify() -> None:
    state = TaskState(objective="x", execution_level=3, required=["verify"])
    state.record_tool(
        "command",
        {"command": "python -m pytest tests -q"},
        success=True,
        result="1 failed, 2 passed",
    )
    assert state.tests == []


def test_source_written_after_empty_commit_does_not_satisfy_changes() -> None:
    state = TaskState(
        objective="implement the fix",
        intent="write_code",
        execution_level=3,
        required=["verify", "commit"],
    )
    state.record_tool(
        "write_file", {"file_path": "tests/test_fix.py"}, success=True
    )
    state.record_tool(
        "command",
        {"command": "git commit -m empty"},
        success=True,
        result="[main abc1234] empty",
    )
    state.record_tool(
        "write_file", {"file_path": "src/fix.py"}, success=True
    )

    assert state.commit == "abc1234"
    assert state.committed_artifacts == ["tests/test_fix.py"]
    assert "changes" in state.missing_delivery()
    assert state.maybe_complete() is False


def test_dirty_worktree_after_commit_is_a_delivery_gap() -> None:
    state = TaskState(
        objective="implement",
        intent="write_code",
        execution_level=3,
        artifacts=["src/app.py"],
        committed_artifacts=["src/app.py"],
        commit="abc1234",
        tests=["pytest -q"],
        required=["verify", "commit"],
    )
    state.record_tool("git", {"git_command": "status"}, success=True, result=" M src/app.py")
    assert "dirty" in state.missing_delivery()
    assert state.maybe_complete() is False

    state.record_tool("git", {"git_command": "status"}, success=True, result="")
    assert "dirty" not in state.missing_delivery()
    assert state.maybe_complete() is True


def test_commit_metadata_limits_committed_artifacts_to_real_paths() -> None:
    state = TaskState(
        objective="implement",
        intent="write_code",
        execution_level=3,
        artifacts=["src/app.py"],
        required=["verify", "commit"],
    )
    state.record_tool(
        "git",
        {"git_command": "commit -m done"},
        success=True,
        result="[main abc1234] done",
        metadata={"committed_paths": ["tests/test_done.py"]},
    )

    assert state.commit == "abc1234"
    assert state.committed_artifacts == ["tests/test_done.py"]
    assert "changes" in state.missing_delivery()


def test_structured_exit_code_verifies_without_passed_text() -> None:
    state = TaskState(objective="x", execution_level=3, required=["verify"])
    state.record_tool(
        "command",
        {"command": "python -m pytest tests -q"},
        success=True,
        result="",
        metadata={"exit_code": 0},
    )
    assert state.tests
    assert state.missing_delivery() == []


def test_failed_verification_invalidates_earlier_pass() -> None:
    state = TaskState(objective="x", execution_level=3, required=["verify"])
    state.record_tool(
        "command",
        {"command": "python -m pytest tests -q"},
        success=True,
        result="10 passed",
        metadata={"exit_code": 0},
    )
    assert state.tests

    state.record_tool(
        "command",
        {"command": "python -m pytest tests -q"},
        success=False,
        result="1 failed, 9 passed",
        metadata={"exit_code": 1},
    )
    assert state.tests == []
    assert state.failed_tests == ["python -m pytest tests -q"]
    assert "verify" in state.missing_delivery()


def test_source_write_after_verification_requires_rerun() -> None:
    state = TaskState(
        objective="x",
        intent="write_code",
        execution_level=3,
        required=["verify"],
    )
    state.record_tool("write_file", {"file_path": "src/app.py"}, success=True)
    state.record_tool(
        "command",
        {"command": "pytest -q"},
        success=True,
        result="1 passed",
        metadata={"exit_code": 0},
    )
    assert state.missing_delivery() == []

    state.record_tool("edit_file", {"file_path": "src/app.py"}, success=True)
    assert "verify" in state.missing_delivery()
    assert "验证已过期" in state.delivery_prompt()


def test_write_after_commit_requires_another_commit() -> None:
    state = TaskState(
        objective="x",
        intent="write_code",
        execution_level=3,
        required=["commit"],
    )
    state.record_tool("write_file", {"file_path": "src/app.py"}, success=True)
    state.record_tool(
        "command",
        {"command": "git commit -m done"},
        success=True,
        result="[main abc1234] done",
        metadata={"committed_paths": ["src/app.py"]},
    )
    assert state.missing_delivery() == []

    state.record_tool("write_file", {"file_path": "src/app.py"}, success=True)
    assert "dirty" in state.missing_delivery()


def test_write_uses_paths_reported_by_tool_metadata() -> None:
    state = TaskState(
        objective="x",
        intent="write_code",
        execution_level=3,
        required=["verify"],
    )
    state.record_tool(
        "write_file",
        {"file_path": "src/requested.py"},
        success=True,
        metadata={"changed_paths": ["src/actual.py"]},
    )
    assert state.artifacts == ["src/actual.py"]


def test_empty_commit_metadata_cannot_bless_precommit_source() -> None:
    state = TaskState(
        objective="implement",
        intent="write_code",
        execution_level=3,
        artifacts=["src/app.py"],
        required=["commit"],
    )
    state.record_tool(
        "git",
        {"git_command": "commit -m empty"},
        success=True,
        result="[main abc1234] empty",
        metadata={"committed_paths": []},
    )
    assert state.commit == "abc1234"
    assert state.committed_artifacts == []
    assert "changes" in state.missing_delivery()


def test_later_test_or_cleanup_commit_keeps_committed_source() -> None:
    state = TaskState(
        objective="implement",
        intent="write_code",
        execution_level=3,
        required=["commit"],
    )
    state.record_tool("write_file", {"file_path": "src/app.py"}, success=True)
    state.record_tool(
        "git",
        {"git_command": "commit -m feat"},
        success=True,
        result="[main abc1234] feat",
        metadata={"committed_paths": ["src/app.py"]},
    )
    assert state.missing_delivery() == []

    state.record_tool("write_file", {"file_path": "tests/test_app.py"}, success=True)
    state.record_tool(
        "git",
        {"git_command": "commit -m tests"},
        success=True,
        result="[main def5678] tests",
        metadata={"committed_paths": ["tests/test_app.py"]},
    )
    assert "changes" not in state.missing_delivery()


def test_source_rewrite_after_commit_reopens_changes_gate() -> None:
    state = TaskState(
        objective="implement",
        intent="write_code",
        execution_level=3,
        required=["commit"],
    )
    state.record_tool("write_file", {"file_path": "src/app.py"}, success=True)
    state.record_tool(
        "git",
        {"git_command": "commit -m feat"},
        success=True,
        result="[main abc1234] feat",
        metadata={"committed_paths": ["src/app.py"]},
    )
    state.record_tool("write_file", {"file_path": "src/app.py"}, success=True)

    assert "changes" in state.missing_delivery()
    assert "dirty" in state.missing_delivery()


def test_same_tool_result_is_applied_once_across_projections() -> None:
    state = TaskState(objective="x", execution_level=2, required=["verify"])
    call = SimpleNamespace(
        tool_name="write_file",
        params={"file_path": "src/app.py"},
        success=True,
        result_summary="written",
        error=None,
        result_id="result-1",
        metadata={},
    )
    state.update_from_calls([call])
    state.update_from_calls([call])

    assert state.artifacts == ["src/app.py"]
    assert state.artifacts_revision == 1


def test_reconcile_delivery_fails_matching_completed_steps() -> None:
    state = TaskState(
        objective="implement",
        intent="write_code",
        execution_level=3,
        steps=[
            {"id": "1", "step": "修改源码", "status": "completed"},
            {"id": "2", "step": "运行 pytest", "status": "completed"},
            {"id": "3", "step": "确认工作区干净", "status": "completed"},
            {"id": "4", "step": "查看文档", "status": "completed"},
        ],
        required=["verify", "commit"],
    )

    state.reconcile_delivery_steps(["changes", "verify", "dirty"])

    assert [step["status"] for step in state.steps] == [
        "failed",
        "failed",
        "failed",
        "completed",
    ]


def test_verification_failure_keeps_real_output_for_repair_prompt() -> None:
    state = TaskState(objective="x", execution_level=3, required=["verify"])
    state.record_tool(
        "command",
        {"command": "pytest tests/test_app.py -q"},
        success=False,
        result="1 failed",
        metadata={
            "exit_code": 1,
            "stdout_tail": "AssertionError: expected 5, got 4",
            "stderr_tail": "at test_app.py:42",
        },
    )

    prompt = state.verification_failure_prompt()
    assert "pytest tests/test_app.py -q" in prompt
    assert "exit_code: 1" in prompt
    assert "AssertionError: expected 5, got 4" in prompt
    assert "at test_app.py:42" in prompt
    assert "失败验证的真实输出" in state.delivery_prompt()

    state.record_tool(
        "command",
        {"command": "pytest tests/test_app.py -q"},
        success=True,
        metadata={"exit_code": 0},
        result="1 passed",
    )
    assert state.verification_failures == []


def test_reconcile_uses_structured_step_kind_not_text() -> None:
    state = TaskState(
        objective="implement",
        intent="write_code",
        execution_level=3,
        steps=[
            {
                "id": "1",
                "step": "do the work",
                "kind": "edit",
                "status": "completed",
            },
            {
                "id": "2",
                "step": "verify it",
                "kind": "verify",
                "status": "completed",
            },
        ],
        required=["verify", "commit"],
    )

    state.reconcile_delivery_steps(["changes"])

    assert state.steps[0]["status"] == "failed"
    assert state.steps[1]["status"] == "completed"

    state.steps.append(
        {
            "id": "3",
            "step": "确认工作区干净",
            "kind": "commit",
            "status": "completed",
        }
    )
    state.reconcile_delivery_steps(["dirty"])
    assert state.steps[2]["status"] == "failed"


def test_plan_revision_archives_old_steps() -> None:
    state = TaskState(objective="x", execution_level=3)
    state.set_plan([{"id": "1", "step": "old", "status": "completed"}])
    state.set_plan(
        [{"id": "2", "step": "new", "status": "pending"}],
        version=2,
        reason="old step failed",
    )

    assert state.plan_version == 2
    assert state.plan_history[0]["version"] == 1
    assert state.plan_history[0]["steps"][0]["step"] == "old"
    assert [step["step"] for step in state.steps] == ["new"]

    state.set_plan(
        [{"id": "0", "step": "stale replay", "status": "pending"}],
        version=1,
    )
    assert [step["step"] for step in state.steps] == ["new"]


def test_failed_remediation_row_is_retired_after_delivery_passes() -> None:
    state = TaskState(
        objective="fix",
        intent="write_code",
        execution_level=3,
        steps=[
            {
                "id": "remediation-1",
                "step": "clean backup file",
                "kind": "clean",
                "status": "failed",
            }
        ],
        tests=["pytest -q"],
        verified_revision=1,
        artifacts=["src/app.py"],
        artifacts_revision=1,
        commit="abc1234",
        committed_artifacts=["src/app.py"],
        committed_source_paths=["src/app.py"],
        source_commit_tracking=True,
        committed_revision=1,
        dirty_after_commit=False,
        required=["verify", "commit"],
    )

    assert state.maybe_complete() is True
    assert state.status == "completed"
    assert state.steps[0]["status"] == "completed"


def test_dirty_status_paths_are_reported_in_repair_prompt() -> None:
    state = TaskState(objective="fix", execution_level=3, required=["commit"])
    state.record_tool(
        "git",
        {"git_command": "status --porcelain"},
        success=True,
        result="?? ops.py.bak\n M src/app.py",
    )

    assert state.dirty_after_commit is True
    assert state.dirty_paths == ["ops.py.bak", "src/app.py"]
    assert "ops.py.bak" in state.delivery_prompt()


def test_new_test_task_rejects_an_empty_test_file() -> None:
    state = TaskState(
        objective="补一个 pytest 回归测试，验证输出格式",
        intent="write_code",
        execution_level=3,
        required=["commit"],
    )
    state.record_tool(
        "write_file",
        {"file_path": "tests/test_new.py", "content": ""},
        success=True,
        metadata={"changed_paths": ["tests/test_new.py"], "content_chars": 0},
    )

    assert state.invalid_test_artifacts == ["tests/test_new.py"]
    assert state.test_artifacts == []
    assert "test" in state.missing_delivery()
    assert "空测试文件" in state.delivery_prompt()

    state.record_tool(
        "write_file",
        {"file_path": "tests/test_new.py", "content": "def test_x():\n    assert 1\n"},
        success=True,
        metadata={"changed_paths": ["tests/test_new.py"], "content_chars": 31},
    )
    assert state.invalid_test_artifacts == []
    assert state.test_artifacts == ["tests/test_new.py"]
    assert "test" not in state.missing_delivery()

    state.record_tool(
        "write_file",
        {"file_path": "tests/test_new.py", "content": ""},
        success=True,
        metadata={"changed_paths": ["tests/test_new.py"], "content_chars": 0},
    )
    assert state.test_artifacts == []
    assert state.invalid_test_artifacts == ["tests/test_new.py"]
    assert "test" in state.missing_delivery()


def test_writing_hardcoded_value_does_not_create_a_test_requirement() -> None:
    state = TaskState(
        objective=(
            "请实现 coverage_summary，数字不得写死；运行 python -m pytest test_app.py -q"
        ),
        execution_level=3,
        tests=["python -m pytest test_app.py -q"],
        verified_revision=0,
        required=["verify"],
    )

    assert state.requires_new_test() is False
    assert "test" not in state.missing_delivery()


def test_staged_and_unstaged_paths_are_distinguished() -> None:
    state = TaskState(objective="fix", execution_level=3)
    state.record_tool(
        "git",
        {"git_command": "status --porcelain"},
        success=True,
        result="M  mathx.py\n M src/app.py\n?? notes.txt",
    )

    assert state.dirty_staged_paths == ["mathx.py"]
    assert state.dirty_unstaged_paths == ["src/app.py", "notes.txt"]
    assert "已暂存但未提交" in state.delivery_prompt()


def test_stale_claim_rejection_from_old_plan_does_not_block_completion() -> None:
    state = TaskState(
        objective="finish",
        execution_level=2,
        plan_version=2,
        steps=[
            {
                "id": "5",
                "step": "write source",
                "kind": "edit",
                "status": "completed",
                "plan_version": 2,
            }
        ],
        claims=[
            {
                "claim_id": "step:5:commit_created:",
                "step_id": "5",
                "kind": "commit_created",
                "status": "rejected",
                "plan_version": 1,
            }
        ],
    )

    assert state.claim_failures() == []
    assert state.maybe_complete() is True


def test_skipped_inspection_step_does_not_mask_verified_delivery() -> None:
    state = TaskState(
        objective="finish",
        execution_level=2,
        steps=[
            {
                "id": "1",
                "step": "inspect",
                "kind": "inspect",
                "status": "skipped",
                "plan_version": 1,
            },
            {
                "id": "2",
                "step": "write",
                "kind": "edit",
                "status": "completed",
                "plan_version": 1,
            },
        ],
    )

    assert state.incomplete_steps() == []
    assert state.maybe_complete() is True
