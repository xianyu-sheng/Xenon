"""Pre-commit staged-source gate tests (#201)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xenon.catalog.gateway import LegacyToolGateway
from xenon.catalog.specs import CORE_SPECS
from xenon.kernel.contracts import Capability, ReceiptStatus, ToolCall
from xenon.nodes.tool_executor import ToolExecuteResult


class FakeExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(
        self, tool: str, params: dict[str, Any], context: Any, tracker: Any
    ) -> ToolExecuteResult:
        self.calls += 1
        return ToolExecuteResult(
            tool_name=tool,
            success=True,
            observation="fake committed",
            raw={"command": params.get("command"), "success": True},
        )


@dataclass
class Services:
    workspace: str
    task_id: str = "t1"
    current_step_id: str | None = None
    verified_paths: tuple[str, ...] | None = ()


def _repo(tmp_path: Path, *, stage: bool = True) -> None:
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, check=True
    )
    if stage:
        subprocess.run(["git", "add", "a.py"], cwd=tmp_path, check=True)


def _gateway(tmp_path: Path) -> tuple[LegacyToolGateway, FakeExecutor]:
    executor = FakeExecutor()
    gateway = LegacyToolGateway(
        executor=executor, specs=CORE_SPECS, workspace=tmp_path
    )
    return gateway, executor


def _commit(capability: Capability = Capability.VCS) -> ToolCall:
    return ToolCall(
        spec_name="bash",
        params={"command": "git commit -m test"},
        capability=capability,
    )


def test_verified_staged_path_can_reach_executor(tmp_path: Path) -> None:
    _repo(tmp_path)
    gateway, executor = _gateway(tmp_path)
    receipt = gateway.execute(
        _commit(),
        Services(str(tmp_path), verified_paths=("a.py",)),
    )

    assert receipt.status is ReceiptStatus.OK
    assert executor.calls == 1


def test_unverified_staged_path_is_denied_before_executor(tmp_path: Path) -> None:
    _repo(tmp_path)
    gateway, executor = _gateway(tmp_path)
    receipt = gateway.execute(
        _commit(),
        Services(str(tmp_path), verified_paths=("b.py",)),
    )

    assert receipt.status is ReceiptStatus.DENIED
    assert "unverified staged paths" in receipt.error
    assert "a.py" in receipt.error
    assert executor.calls == 0


def test_empty_staged_diff_is_denied(tmp_path: Path) -> None:
    _repo(tmp_path, stage=False)
    gateway, executor = _gateway(tmp_path)
    receipt = gateway.execute(
        _commit(),
        Services(str(tmp_path), verified_paths=("a.py",)),
    )

    assert receipt.status is ReceiptStatus.DENIED
    assert "staged diff is empty" in receipt.error
    assert executor.calls == 0


def test_unsafe_commit_command_is_denied(tmp_path: Path) -> None:
    _repo(tmp_path)
    gateway, executor = _gateway(tmp_path)
    call = ToolCall(
        spec_name="bash",
        params={"command": "git commit -m test && echo pwned"},
        capability=Capability.VCS,
    )
    receipt = gateway.execute(
        call, Services(str(tmp_path), verified_paths=("a.py",))
    )

    assert receipt.status is ReceiptStatus.DENIED
    assert "unsafe commit" in receipt.error
    assert executor.calls == 0


def test_alternate_worktree_commit_is_denied(tmp_path: Path) -> None:
    _repo(tmp_path)
    gateway, executor = _gateway(tmp_path)
    call = ToolCall(
        spec_name="bash",
        params={"command": "git --work-tree . commit -m test"},
        capability=Capability.VCS,
    )
    receipt = gateway.execute(
        call, Services(str(tmp_path), verified_paths=("a.py",))
    )

    assert receipt.status is ReceiptStatus.DENIED
    assert "alternate repository" in receipt.error
    assert executor.calls == 0
