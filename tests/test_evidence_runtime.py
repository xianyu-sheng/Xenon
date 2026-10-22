"""完整 Evidence Runtime 的不可变事件与账本测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from xenon.engine.evidence_runtime import (
    EventKind,
    EvidenceEvent,
    EvidenceLedger,
    EvidencePack,
    EvidenceRuntime,
    EvidenceSource,
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


def test_ledger_subscription_receives_appended_events() -> None:
    ledger = EvidenceLedger("run-sub")
    received: list[EvidenceEvent] = []
    ledger.subscribe(received.append)

    event = ledger.append(
        LifecyclePhase.POST_TOOL,
        EventKind.TOOL_OBSERVATION,
        EvidenceSource.TOOL,
        {"tool": "command"},
    )

    assert received == [event]


def test_ledger_subscription_filters_and_replays() -> None:
    ledger = EvidenceLedger("run-filter")
    ledger.append(
        LifecyclePhase.TASK,
        EventKind.TASK_FACT,
        EvidenceSource.ENGINE,
    )
    observations: list[EvidenceEvent] = []
    subscription = ledger.subscribe(
        observations.append,
        kind=EventKind.TOOL_OBSERVATION,
        source="tool",
        replay=True,
    )
    assert observations == []

    ledger.append(
        LifecyclePhase.POST_TOOL,
        EventKind.TOOL_OBSERVATION,
        EvidenceSource.TOOL,
    )
    ledger.append(
        LifecyclePhase.VALIDATION,
        EventKind.VALIDATION_RESULT,
        EvidenceSource.TEST,
    )
    assert len(observations) == 1
    assert observations[0].kind == EventKind.TOOL_OBSERVATION.value

    ledger.unsubscribe(subscription)
    ledger.append(
        LifecyclePhase.POST_TOOL,
        EventKind.TOOL_OBSERVATION,
        EvidenceSource.TOOL,
    )
    assert len(observations) == 1


def test_subscriber_failure_is_isolated(caplog) -> None:
    ledger = EvidenceLedger("run-isolation")
    received: list[EvidenceEvent] = []

    def broken(_event: EvidenceEvent) -> None:
        raise RuntimeError("projection failed")

    ledger.subscribe(broken)
    ledger.subscribe(received.append)
    event = ledger.append(
        LifecyclePhase.DELIVERY,
        EventKind.DELIVERY,
        EvidenceSource.ENGINE,
    )

    assert received == [event]
    assert any("EvidenceLedger subscriber" in record.message for record in caplog.records)


def test_runtime_subscription_facade() -> None:
    runtime = EvidenceRuntime(EvidenceLedger("run-facade"))
    received: list[EvidenceEvent] = []
    runtime.subscribe(received.append, replay=True)
    event = runtime.record_validation(kind="pytest", passed=True, detail="2 passed")

    assert received == [event]
    assert runtime.ledger.verify_integrity()
