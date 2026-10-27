"""Tests for the legacy ToolExecutor -> ToolReceipt adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xenon.catalog.gateway import LegacyToolGateway
from xenon.catalog.specs import CORE_SPECS, LEGACY_SPECS
from xenon.kernel.contracts import (
    EffectKind,
    FsWrite,
    ReadFact,
    ReceiptStatus,
    TestRun,
    ToolCall,
    VcsCommit,
)
from xenon.nodes.tool_executor import ToolExecuteResult


class FakeExecutor:
    def __init__(self, raw: dict[str, Any], *, error: str | None = None) -> None:
        self.raw = raw
        self.error = error

    def execute(
        self,
        tool: str,
        params: dict[str, Any],
        context: Any,
        tracker: Any,
    ) -> ToolExecuteResult:
        if self.error:
            raise RuntimeError(self.error)
        return ToolExecuteResult(
            tool_name=tool,
            success=bool(self.raw.get("success", True)),
            observation="ok",
            raw=self.raw,
        )


@dataclass
class Services:
    workspace: str
    task_id: str = "t1"
    current_step_id: str | None = "s1"


def _gateway(
    tmp_path: Path, raw: dict[str, Any], *, error: str | None = None
) -> LegacyToolGateway:
    return LegacyToolGateway(
        executor=FakeExecutor(raw, error=error),
        specs=CORE_SPECS + LEGACY_SPECS,
        workspace=tmp_path,
    )


def test_apply_patch_becomes_fs_write_with_created_path(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        {"success": True, "changed": True, "changed_paths": ["a.py"]},
    )
    receipt = gateway.execute(
        ToolCall(
            spec_name="apply_patch",
            params={"files": [{"path": "a.py", "content": "x"}]},
        ),
        Services(workspace=str(tmp_path)),
    )
    assert receipt.status is ReceiptStatus.OK
    write = receipt.effects[0]
    assert isinstance(write, FsWrite)
    assert write.paths == ("a.py",)
    assert write.changed is True
    assert write.created_paths == ("a.py",)
    assert receipt.declared_effects == (EffectKind.FS_WRITE,)
    assert EffectKind.FS_WRITE in receipt.observed_effects


def test_noop_write_stays_a_noop_fact(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        {"success": True, "changed": False, "changed_paths": ["a.py"]},
    )
    receipt = gateway.execute(
        ToolCall(spec_name="apply_patch", params={"file_path": "a.py"}),
        Services(workspace=str(tmp_path)),
    )
    write = receipt.effects[0]
    assert isinstance(write, FsWrite)
    assert write.changed is False


def test_bash_pytest_becomes_test_run_with_real_target(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        {
            "success": True,
            "command": "python -m pytest tests/test_calc.py -q",
            "tests_run": 2,
            "tests_passed": 2,
            "tests_failed": 0,
            "tests_errors": 0,
            "exit_code": 0,
        },
    )
    receipt = gateway.execute(
        ToolCall(
            spec_name="bash",
            params={"command": "python -m pytest tests/test_calc.py -q"},
        ),
        Services(workspace=str(tmp_path)),
    )
    runs = [item for item in receipt.effects if isinstance(item, TestRun)]
    assert len(runs) == 1
    assert runs[0].target == "tests/test_calc.py"
    assert runs[0].tests_run == 2
    assert runs[0].passed == 2


def test_bash_git_commit_becomes_vcs_commit(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        {
            "success": True,
            "command": "git commit -m fix",
            "stdout": "[main abc1234] fix",
            "committed_paths": ["a.py"],
        },
    )
    receipt = gateway.execute(
        ToolCall(spec_name="bash", params={"command": "git commit -m fix"}),
        Services(workspace=str(tmp_path)),
    )
    commits = [item for item in receipt.effects if isinstance(item, VcsCommit)]
    assert len(commits) == 1
    assert commits[0].sha == "abc1234"
    assert commits[0].files == ("a.py",)


def test_git_status_is_not_a_commit(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        {"success": True, "command": "git status --short", "stdout": ""},
    )
    receipt = gateway.execute(
        ToolCall(spec_name="bash", params={"command": "git status --short"}),
        Services(workspace=str(tmp_path)),
    )
    assert not [item for item in receipt.effects if isinstance(item, VcsCommit)]


def test_read_becomes_read_fact_with_content_hash(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, {"success": True, "content": "hello"})
    receipt = gateway.execute(
        ToolCall(spec_name="read", params={"file_path": "a.py"}),
        Services(workspace=str(tmp_path)),
    )
    reads = [item for item in receipt.effects if isinstance(item, ReadFact)]
    assert len(reads) == 1
    assert reads[0].path == "a.py"
    assert reads[0].content_hash
    assert reads[0].content == "hello"


def test_executor_exception_becomes_a_failed_receipt(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, {}, error="boom")
    receipt = gateway.execute(
        ToolCall(spec_name="read", params={"file_path": "a.py"}),
        Services(workspace=str(tmp_path)),
    )
    assert receipt.status is ReceiptStatus.FAILED
    assert "boom" in receipt.error


def test_legacy_run_tests_receipt_uses_structured_target(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        {
            "success": True,
            "tests_run": 1,
            "tests_passed": 1,
            "exit_code": 0,
            "test_paths": ["tests/test_a.py"],
        },
    )
    receipt = gateway.execute(
        ToolCall(
            spec_name="run_tests",
            params={"test_paths": ["tests/test_a.py"]},
        ),
        Services(workspace=str(tmp_path)),
    )
    run = [item for item in receipt.effects if isinstance(item, TestRun)][0]
    assert run.target == "tests/test_a.py"
