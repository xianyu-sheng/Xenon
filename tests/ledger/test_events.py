"""Tests for typed events and content-addressed IDs."""

from __future__ import annotations

from xenon.kernel.contracts import (
    FsWrite,
    ReceiptStatus,
    TestRun,
    ToolReceipt,
    VcsCommit,
)
from xenon.ledger.events import (
    Event,
    EventKind,
    EventSource,
    effect_from_payload,
    effect_to_payload,
    fact_id,
    receipt_event_payload,
    receipt_fact_ids,
    receipt_from_payload,
)

TS = "2026-01-01T00:00:00.000+00:00"


def _event(**overrides):
    values = {
        "seq": 1,
        "task_id": "t1",
        "kind": EventKind.TASK_STARTED,
        "source": EventSource.RUNTIME,
        "payload": {"objective": "fix add"},
        "prev_hash": "",
        "ts": TS,
    }
    values.update(overrides)
    return Event.create(**values)


def test_event_hash_is_deterministic_and_roundtrips() -> None:
    first = _event()
    second = _event()
    assert first.hash == second.hash
    assert first.verify_hash()
    assert Event.from_dict(first.to_dict()).hash == first.hash


def test_event_hash_changes_with_payload() -> None:
    first = _event()
    second = _event(payload={"objective": "fix mul"})
    assert first.hash != second.hash
    assert second.verify_hash()


def test_event_freezes_its_payload() -> None:
    payload: dict[str, list[int]] = {"a": [1]}
    event = _event(payload=payload)
    payload["a"].append(2)
    assert event.payload == {"a": [1]}
    assert event.verify_hash()


def test_effect_payload_roundtrip() -> None:
    effect = FsWrite(paths=("calc/add.py",), created_paths=("calc/add.py",))
    payload = effect_to_payload(effect)
    assert payload["type"] == "fs_write"
    assert effect_from_payload(payload) == effect


def test_receipt_payload_roundtrip_and_fact_ids() -> None:
    receipt = ToolReceipt(
        receipt_id="r1",
        call_id="c1",
        task_id="t1",
        tool="mutate",
        status=ReceiptStatus.OK,
        effects=(
            FsWrite(paths=("calc/add.py",), created_paths=("calc/add.py",)),
            TestRun(target="tests/test_add.py", tests_run=1, passed=1),
            VcsCommit(sha="abc1234", files=("calc/add.py",)),
        ),
        declared_effects=(),
        observed_effects=(),
    )
    payload = receipt_event_payload(receipt)
    assert payload["fact_ids"] == list(receipt_fact_ids(receipt))
    restored = receipt_from_payload(payload["receipt"])
    assert restored.receipt_id == receipt.receipt_id
    assert restored.effects[0] == receipt.effects[0]
    assert restored.effects[1] == receipt.effects[1]


def test_fact_ids_are_content_addressed() -> None:
    left = effect_to_payload(FsWrite(paths=("a.py",)))
    right = effect_to_payload(FsWrite(paths=("a.py",)))
    other = effect_to_payload(FsWrite(paths=("b.py",)))
    assert fact_id(left) == fact_id(right)
    assert fact_id(left) != fact_id(other)
    assert fact_id(left).startswith("fact-")


def test_hmac_signs_and_verifies() -> None:
    event = _event(hmac_key=b"secret")
    assert event.mac
    assert event.verify_mac(b"secret")
    assert not event.verify_mac(b"other")
