"""Finalize guard tests (#201)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from tests.kernel.test_north_star import (
    FakeCatalog,
    FakeGateway,
    ScriptedStrategy,
    _calc_spec,
)
from xenon.kernel.contracts import (
    CallTool,
    Capability,
    EffectKind,
    Finish,
    FsWrite,
    ReceiptStatus,
    ToolIntent,
    ToolReceipt,
    VcsCommit,
    VcsPush,
)
from xenon.kernel.controller import VerificationController
from xenon.kernel.finalize import FinalizeGuard
from xenon.kernel.runtime import LoopRuntime
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


def _append(ledger: Ledger, *effects) -> None:
    receipt = ToolReceipt(
        receipt_id=f"r-{ledger.watermark() + 1}",
        call_id=f"c-{ledger.watermark() + 1}",
        task_id="t1",
        tool="tool",
        status=ReceiptStatus.OK,
        effects=tuple(effects),
        declared_effects=tuple(item.kind for item in effects),
        observed_effects=tuple(item.kind for item in effects),
    )
    ledger.append_receipt(receipt)


def test_verified_write_and_commit_pass(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("print('a')\n", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    ledger = Ledger.in_memory("t1")
    _append(ledger, FsWrite(paths=("a.py",), hashes={"a.py": digest}))
    _append(ledger, VcsCommit(sha="abc1234", files=("a.py",)))

    decision = FinalizeGuard().check(ledger, workspace=tmp_path)

    assert decision.accepted is True
    assert decision.commit_sha == "abc1234"
    assert decision.verified_paths == ("a.py",)


def test_commit_with_unverified_path_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("a\n", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    ledger = Ledger.in_memory("t1")
    _append(ledger, FsWrite(paths=("a.py",), hashes={"a.py": digest}))
    _append(
        ledger,
        VcsCommit(sha="abc1234", files=("a.py", "outside.py")),
    )

    decision = FinalizeGuard().check(ledger, workspace=tmp_path)

    assert decision.accepted is False
    assert decision.unverified_paths == ("outside.py",)


def test_file_changed_after_verification_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("before\n", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    ledger = Ledger.in_memory("t1")
    _append(ledger, FsWrite(paths=("a.py",), hashes={"a.py": digest}))
    _append(ledger, VcsCommit(sha="abc1234", files=("a.py",)))
    target.write_text("after\n", encoding="utf-8")

    decision = FinalizeGuard().check(ledger, workspace=tmp_path)

    assert decision.accepted is False
    assert decision.hash_mismatches == ("a.py",)


def test_commit_path_outside_workspace_is_rejected(tmp_path: Path) -> None:
    ledger = Ledger.in_memory("t1")
    _append(ledger, FsWrite(paths=("../outside.py",)))
    _append(
        ledger,
        VcsCommit(sha="abc1234", files=("../outside.py",)),
    )

    decision = FinalizeGuard().check(ledger, workspace=tmp_path)

    assert decision.accepted is False
    assert "../outside.py" in decision.unverified_paths


def test_commit_without_file_manifest_is_rejected(tmp_path: Path) -> None:
    ledger = Ledger.in_memory("t1")
    _append(ledger, VcsCommit(sha="abc1234"))

    decision = FinalizeGuard().check(ledger, workspace=tmp_path)

    assert decision.accepted is False
    assert "manifest" in decision.reason


def test_push_is_rejected_unless_explicit(tmp_path: Path) -> None:
    ledger = Ledger.in_memory("t1")
    _append(ledger, VcsPush(remote="origin", branch="main", sha="abc1234"))

    denied = FinalizeGuard().check(ledger, workspace=tmp_path)
    allowed = FinalizeGuard().check(
        ledger, workspace=tmp_path, allow_push=True
    )

    assert denied.accepted is False
    assert "push" in denied.reason
    assert allowed.accepted is True


class UnverifiedCommitGateway(FakeGateway):
    def execute(self, call, services):
        if call.spec_name == "record":
            return ToolReceipt(
                receipt_id="bad-record",
                call_id="bad-record-call",
                task_id="task-1",
                tool="record",
                status=ReceiptStatus.OK,
                effects=(
                    VcsCommit(sha="abc1234", files=("outside.py",)),
                ),
                declared_effects=(EffectKind.VCS_COMMIT,),
                observed_effects=(EffectKind.VCS_COMMIT,),
            )
        return super().execute(call, services)


def test_runtime_blocks_delivery_when_commit_has_unverified_path() -> None:
    ledger = Ledger.in_memory("task-1")
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=UnverifiedCommitGateway(),
        controller=VerificationController(),
        ledger=ledger,
    )
    result = runtime.run(
        _calc_spec(),
        ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.READ)),
                CallTool(ToolIntent(capability=Capability.EDIT)),
                CallTool(ToolIntent(capability=Capability.VCS)),
                Finish("done"),
            ]
        ),
    )

    assert result.accepted is False
    assert result.phase.value == "blocked"
    assert any(item.obligation.id == "finalize" for item in result.debt)
    assert any(
        event.kind is EventKind.FINALIZE_STARTED for event in ledger.read()
    )


def test_non_sha_fake_hash_does_not_trigger_hash_check(tmp_path: Path) -> None:
    ledger = Ledger.in_memory("t1")
    _append(ledger, FsWrite(paths=("a.py",), hashes={"a.py": "h2"}))
    _append(ledger, VcsCommit(sha="abc1234", files=("a.py",)))

    assert FinalizeGuard().check(ledger, workspace=tmp_path).accepted is True
