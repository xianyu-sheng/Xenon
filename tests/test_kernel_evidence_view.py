"""Kernel ledger views for /evidence and /resume (#236)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from xenon.kernel.contracts import (
    FsWrite,
    ReceiptStatus,
    TestRun,
    ToolReceipt,
)
from xenon.ledger.events import (
    EventKind,
    EventSource,
    receipt_event_payload,
)
from xenon.ledger.store import Ledger
from xenon.repl.command_groups.evidence import _cmd_evidence
from xenon.repl.kernel_evidence import (
    load_kernel_evidence,
    render_kernel_evidence,
    restore_session_pointer,
    snapshot_for_session,
)


def _make_ledger(directory: Path, task_id: str = "kernel-1") -> Path:
    path = directory / f"{task_id}.jsonl"
    ledger = Ledger(path, task_id)
    ledger.append(
        EventKind.TASK_STARTED,
        EventSource.RUNTIME,
        {"objective": "fix add", "workspace": str(directory)},
    )
    receipt = ToolReceipt(
        receipt_id="r-write",
        call_id="c-write",
        task_id=task_id,
        tool="apply_patch",
        status=ReceiptStatus.OK,
        effects=(
            FsWrite(paths=("calc/add.py",), hashes={"calc/add.py": "h"}),
            TestRun(
                target="tests/test_add.py",
                tests_run=1,
                passed=1,
                exit_code=0,
            ),
        ),
    )
    ledger.append(
        EventKind.TOOL_RECEIPT,
        EventSource.TOOL,
        receipt_event_payload(receipt),
    )
    ledger.append(
        EventKind.CLAIM_PROPOSED,
        EventSource.LLM,
        {"claim_id": "c1", "kind": "file_changed", "target": "calc/add.py"},
    )
    ledger.append(
        EventKind.CLAIM_RESOLVED,
        EventSource.GATE,
        {"claim_id": "c1", "status": "bound", "fact_ids": ["f1"]},
    )
    ledger.append(
        EventKind.DELIVERED,
        EventSource.RUNTIME,
        {"answer": "fixed add()", "claims": []},
    )
    ledger.append(
        EventKind.FINALIZED,
        EventSource.RUNTIME,
        {"commit": "abc1234", "evidence_status": "verified"},
    )
    path.with_suffix(".result.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "accepted": True,
                "phase": "finalized",
                "evidence_status": "verified",
                "finalize_sha": "abc1234",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_kernel_evidence_human_and_raw_views(
    tmp_path: Path, monkeypatch
) -> None:
    import xenon.repl.kernel_evidence as module

    monkeypatch.setattr(module, "kernel_ledger_dir", lambda: tmp_path)
    path = _make_ledger(tmp_path)
    evidence = load_kernel_evidence(path)
    assert evidence is not None
    assert evidence.integrity.valid
    human = render_kernel_evidence(evidence)
    assert "Kernel evidence" in human
    assert "fix add" in human
    assert "测试" in human
    assert "abc1234" in human
    assert "链: ✅" in human

    raw = render_kernel_evidence(evidence, raw=True)
    assert "raw" in raw
    assert "tool_receipt" in raw
    assert "h=" in raw


def test_kernel_evidence_rejects_tampering(
    tmp_path: Path, monkeypatch
) -> None:
    import xenon.repl.kernel_evidence as module

    monkeypatch.setattr(module, "kernel_ledger_dir", lambda: tmp_path)
    path = _make_ledger(tmp_path, "kernel-tamper")
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["payload"]["objective"] = "tampered"
    lines[0] = json.dumps(record, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    evidence = load_kernel_evidence(path)
    assert evidence is not None
    assert evidence.integrity.valid is False
    assert "链: ❌" in render_kernel_evidence(evidence)


def test_evidence_command_prefers_kernel_pointer(
    tmp_path: Path, monkeypatch
) -> None:
    import xenon.repl.kernel_evidence as module

    monkeypatch.setattr(module, "kernel_ledger_dir", lambda: tmp_path)
    path = _make_ledger(tmp_path, "kernel-cmd")
    repl = SimpleNamespace(
        _last_kernel_ledger=path,
        _session_events=SimpleNamespace(read=lambda: [], path=tmp_path / "legacy"),
    )
    out = _cmd_evidence(args="", session_state={"_repl": repl})
    assert "Kernel evidence" in out
    assert "kernel-cmd" in out
    assert "fix add" in out

    raw = _cmd_evidence(args="kernel raw", session_state={"_repl": repl})
    assert "Kernel evidence（raw）" in raw
    assert "tool_receipt" in raw

    named = _cmd_evidence(args="kernel-cmd", session_state={"_repl": repl})
    assert "Kernel evidence" in named


def test_evidence_global_lists_kernel_ledgers(tmp_path: Path, monkeypatch) -> None:
    import xenon.repl.kernel_evidence as module
    import xenon.session.events as events_mod

    monkeypatch.setattr(module, "kernel_ledger_dir", lambda: tmp_path)
    monkeypatch.setattr(events_mod, "session_events_dir", lambda: tmp_path / "none")
    _make_ledger(tmp_path, "kernel-all")
    out = _cmd_evidence(args="all", session_state={})
    assert "Kernel ledgers" in out
    assert "kernel-all" in out


def test_kernel_pointer_snapshot_and_restore(tmp_path: Path, monkeypatch) -> None:
    import xenon.repl.kernel_evidence as module

    monkeypatch.setattr(module, "kernel_ledger_dir", lambda: tmp_path)
    path = _make_ledger(tmp_path, "kernel-pointer")
    repl = SimpleNamespace(_last_kernel_ledger=path)
    pointer = snapshot_for_session(repl)
    assert pointer is not None
    assert pointer["task_id"] == "kernel-pointer"

    restored = SimpleNamespace(_last_kernel_ledger=None)
    assert restore_session_pointer(restored, {"kernel_evidence": pointer})
    assert restored._last_kernel_ledger == path
    assert restored._last_kernel_task_id == "kernel-pointer"

    outside = tmp_path.parent / "outside.jsonl"
    outside.write_text("{}", encoding="utf-8")
    assert not restore_session_pointer(
        restored, {"kernel_evidence": {"ledger": str(outside)}}
    )
    assert restored._last_kernel_ledger is None


def test_auto_save_persists_kernel_pointer(tmp_path: Path, monkeypatch) -> None:
    import xenon.repl.kernel_evidence as module
    from xenon.repl import session as session_mod
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.repl import REPL

    monkeypatch.setattr(module, "kernel_ledger_dir", lambda: tmp_path)
    monkeypatch.setattr(session_mod, "SESSIONS_DIR", tmp_path / "sessions")
    path = _make_ledger(tmp_path, "kernel-auto")
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._last_kernel_ledger = path
    repl._auto_save_session(cleanup=False)

    data = session_mod.load_session("_auto")
    pointer = data["extra"]["kernel_evidence"]
    assert pointer["task_id"] == "kernel-auto"
    assert pointer["ledger"] == str(path)


def test_save_and_load_preserve_kernel_pointer(
    tmp_path: Path, monkeypatch
) -> None:
    import xenon.repl.kernel_evidence as module
    from xenon.repl import session as session_mod
    from xenon.repl.command_groups import session as command_session
    from xenon.repl.command_groups.session import _cmd_load, _cmd_resume, _cmd_save
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.repl import REPL

    monkeypatch.setattr(module, "kernel_ledger_dir", lambda: tmp_path)
    monkeypatch.setattr(session_mod, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(command_session, "confirm_action", lambda *a, **k: True)
    path = _make_ledger(tmp_path, "kernel-saved")

    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._last_kernel_ledger = path
    session_state = {"_repl": repl, "agent_context": repl.agent_context}
    result = _cmd_save(
        args="kernel-save",
        ctx_mgr=repl.ctx_mgr,
        registry=registry,
        session_state=session_state,
    )
    assert "已保存" in result
    data = session_mod.load_session("kernel-save")
    assert data["extra"]["kernel_evidence"]["task_id"] == "kernel-saved"

    restored = REPL(registry=registry, streaming=False)
    assert restored._last_kernel_ledger is None
    _cmd_load(
        args="kernel-save",
        ctx_mgr=restored.ctx_mgr,
        registry=registry,
        session_state={"_repl": restored, "agent_context": restored.agent_context},
    )
    assert restored._last_kernel_ledger == path
    assert restored._last_kernel_task_id == "kernel-saved"

    resumed = REPL(registry=registry, streaming=False)
    _cmd_resume(args="kernel-save", session_state={"_repl": resumed})
    assert resumed._last_kernel_ledger == path
