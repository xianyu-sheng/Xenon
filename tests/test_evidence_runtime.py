"""完整 Evidence Runtime 的不可变事件与账本测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from xenon.engine.evidence_runtime import (
    EvidenceEvent,
    EvidenceLedger,
    EvidencePack,
    EvidenceSource,
    EventKind,
    LifecyclePhase,
)


def test_event_canonical_hash_and_copy() -> None:
    payload = {"tool": "read_file", "params": {"file_path": "src/a.py"}}
    event = EvidenceEvent.create(
        session_id="run-1",
        phase=LifecyclePhase.UNDERSTANDING,
        kind=EventKind.TOOL_OBSERVATION,
        source=EvidenceSource.TOOL,
        payload=payload,
    )
    payload["params"]["file_path"] = "changed.py"
    assert event.payload["params"]["file_path"] == "src/a.py"
    assert event.verify_integrity()
    assert event.to_dict()["content_hash"] == event.content_hash


def test_ledger_orders_links_and_queries() -> None:
    ledger = EvidenceLedger("run-1")
    a = ledger.append(
        LifecyclePhase.TASK, EventKind.TASK_FACT, EvidenceSource.USER, {"goal": "fix"}
    )
    b = ledger.append(
        LifecyclePhase.UNDERSTANDING,
        EventKind.TOOL_OBSERVATION,
        EvidenceSource.TOOL,
        {"path": "a.py"},
    )
    assert (a.sequence, b.sequence) == (1, 2)
    assert b.predecessor_id == a.event_id
    assert ledger.query(phase=LifecyclePhase.UNDERSTANDING) == [b]
    assert ledger.verify_integrity()


def test_ledger_rejects_tampering_and_bad_append() -> None:
    ledger = EvidenceLedger("run-1")
    event = ledger.append(
        LifecyclePhase.TASK, EventKind.TASK_FACT, EvidenceSource.USER, {}
    )
    event.payload["x"] = 1
    assert not ledger.verify_integrity()
    other = EvidenceEvent.create(
        session_id="run-2",
        phase=LifecyclePhase.TASK,
        kind=EventKind.TASK_FACT,
        source=EvidenceSource.USER,
        payload={},
    )
    with pytest.raises(ValueError):
        ledger.append_event(other)


def test_snapshot_restore_and_jsonl_roundtrip(tmp_path: Path) -> None:
    ledger = EvidenceLedger("run-1")
    ledger.append(
        LifecyclePhase.TASK, EventKind.TASK_FACT, EvidenceSource.USER, {"x": 1}
    )
    ledger.append(
        LifecyclePhase.VALIDATION,
        EventKind.GATE_VERDICT,
        EvidenceSource.GATE,
        {"passed": True},
    )
    snapshot = ledger.snapshot()
    restored = EvidenceLedger.from_snapshot(snapshot)
    assert restored.verify_integrity()
    path = tmp_path / "ledger.jsonl"
    ledger.write_jsonl(path)
    assert EvidenceLedger.read_jsonl(path).snapshot() == snapshot


def test_pack_summarizes_gates_and_requires_integrity() -> None:
    ledger = EvidenceLedger("run-1")
    ledger.append(
        LifecyclePhase.TASK, EventKind.TASK_FACT, EvidenceSource.USER, {"goal": "fix"}
    )
    ledger.append(
        LifecyclePhase.VALIDATION,
        EventKind.GATE_VERDICT,
        EvidenceSource.GATE,
        {"gate": "fact", "passed": False},
    )
    pack = EvidencePack.build(ledger)
    assert pack.session_id == "run-1"
    assert pack.gate_failures == 1
    assert pack.event_count == 2
    assert pack.to_dict()["integrity_verified"] is True


def test_event_kind_and_phase_are_closed_protocol_values() -> None:
    with pytest.raises(ValueError):
        EvidenceEvent.create(
            session_id="r",
            phase="unknown",
            kind=EventKind.TASK_FACT,
            source=EvidenceSource.USER,
            payload={},
        )
    with pytest.raises(ValueError):
        EvidenceEvent.create(
            session_id="r",
            phase=LifecyclePhase.TASK,
            kind="unknown",
            source=EvidenceSource.USER,
            payload={},
        )
