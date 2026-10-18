"""#163 回归：改完代码未验证时给出一次性提醒，验证过就不打扰。"""

from __future__ import annotations

from xenon.engine.tool_tracker import ToolCall
from xenon.engine.verification_nudge import (
    is_verification_command,
    needs_verification,
)


def _call(name: str, *, success: bool = True, **params) -> ToolCall:
    return ToolCall(
        tool_name=name,
        params=dict(params),
        success=success,
        result_summary="",
    )


def test_edit_without_verification_needs_nudge() -> None:
    calls = [_call("read_file"), _call("edit_file", file_path="a.py")]
    assert needs_verification(calls) is True


def test_edit_then_pytest_is_verified() -> None:
    calls = [
        _call("edit_file", file_path="a.py"),
        _call("command", action="python -m pytest tests -q"),
    ]
    assert needs_verification(calls) is False


def test_verification_before_edit_does_not_count() -> None:
    calls = [
        _call("command", action="pytest -q"),
        _call("write_file", file_path="a.py"),
    ]
    assert needs_verification(calls) is True


def test_failed_write_does_not_trigger_nudge() -> None:
    calls = [_call("write_file", success=False, file_path="a.py")]
    assert needs_verification(calls) is False


def test_failed_test_run_still_counts_as_attempt() -> None:
    """测试跑红了也算"验证过"：反馈已经交给模型，重复催促没有信息量。"""

    calls = [
        _call("edit_file", file_path="a.py"),
        _call("command", success=False, action="pytest -q"),
    ]
    assert needs_verification(calls) is False


def test_read_every_verification_command_families() -> None:
    for command in (
        "pytest -q",
        "python -m compileall src",
        "python3 -m unittest discover",
        "npm test",
        "npm run test",
        "yarn test",
        "go test ./...",
        "cargo test",
        "make check",
        "ruff check .",
        "mypy xenon",
        "npx tsc --noEmit",
    ):
        assert is_verification_command(_call("command", action=command)), command


def test_read_only_commands_are_not_verification() -> None:
    for command in ("ls -la", "git status", "cat a.py", "python app.py"):
        assert not is_verification_command(_call("command", action=command)), command


def test_temp_script_write_does_not_trigger_nudge() -> None:
    calls = [
        _call("write_file", file_path=".xenon_tmp/list_issues.ps1"),
        _call("read_file", file_path="README.md"),
    ]
    assert needs_verification(calls) is False


def test_repo_script_write_still_triggers_nudge() -> None:
    calls = [_call("write_file", file_path="scripts/check.py")]
    assert needs_verification(calls) is True
