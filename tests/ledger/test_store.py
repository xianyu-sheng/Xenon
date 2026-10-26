"""Tests for the append-only ledger store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.store import Ledger, LedgerError, ReadOnlyLedger

TS = "2026-01-01T00:00:00.000+00:00"


def _ledger(tmp_path: Path, hmac_key: bytes | None = None) -> Ledger:
    return Ledger.open(tmp_path / "task.jsonl", task_id="t1", hmac_key=hmac_key)


def _append(ledger: Ledger, count: int = 3) -> None:
    for index in range(count):
        ledger.append(
            EventKind.USER_INPUT,
            EventSource.USER,
            {"i": index},
            ts=TS,
        )


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def test_append_read_watermark_and_anchor(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)

    assert ledger.watermark() == 3
    assert [event.seq for event in ledger.read()] == [1, 2, 3]
    assert [event.seq for event in ledger.read(EventKind.USER_INPUT)] == [1, 2, 3]
    assert [event.seq for event in ledger.read(up_to_seq=2)] == [1, 2]
    assert ledger.verify().valid is True
    assert (tmp_path / "task.jsonl.anchor").exists()


def test_reopen_continues_the_sequence(tmp_path: Path) -> None:
    _append(_ledger(tmp_path), 2)
    reopened = Ledger.open(tmp_path / "task.jsonl", task_id="t1")
    assert reopened.watermark() == 2
    reopened.append(EventKind.USER_INPUT, EventSource.USER, {"i": 2}, ts=TS)
    assert reopened.watermark() == 3
    assert reopened.verify().valid is True


def test_tampered_payload_is_detected(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger, 2)
    path = tmp_path / "task.jsonl"
    lines = _lines(path)
    data = json.loads(lines[0])
    data["payload"]["i"] = 99
    lines[0] = json.dumps(data, sort_keys=True) + "\n"
    path.write_text("".join(lines), encoding="utf-8")

    report = Ledger.open(path, task_id="t1").verify()
    assert report.valid is False
    assert "hash" in report.reason


def test_reordered_events_are_detected(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger, 3)
    path = tmp_path / "task.jsonl"
    lines = _lines(path)
    lines[0], lines[1] = lines[1], lines[0]
    path.write_text("".join(lines), encoding="utf-8")

    assert Ledger.open(path, task_id="t1").verify().valid is False


def test_deleted_middle_event_is_detected(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger, 3)
    path = tmp_path / "task.jsonl"
    lines = _lines(path)
    del lines[1]
    path.write_text("".join(lines), encoding="utf-8")

    report = Ledger.open(path, task_id="t1").verify()
    assert report.valid is False
    assert "seq" in report.reason


def test_truncated_tail_is_detected_by_anchor(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger, 3)
    path = tmp_path / "task.jsonl"
    lines = _lines(path)
    path.write_text("".join(lines[:-1]), encoding="utf-8")

    report = Ledger.open(path, task_id="t1").verify()
    assert report.valid is False
    assert "shorter than its anchor" in report.reason


def test_incomplete_tail_can_be_recovered(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger, 2)
    path = tmp_path / "task.jsonl"
    with path.open("ab") as handle:
        handle.write(b'{"seq":3')

    opened = Ledger.open(path, task_id="t1")
    assert opened.truncated_tail is True
    assert opened.verify().valid is False
    with pytest.raises(LedgerError):
        opened.append(EventKind.USER_INPUT, EventSource.USER, {"i": 9})

    corrupt = opened.recover_truncated()
    assert corrupt is not None and corrupt.exists()
    opened.append(EventKind.USER_INPUT, EventSource.USER, {"i": 2}, ts=TS)
    assert opened.watermark() == 3
    assert Ledger.open(path, task_id="t1").verify().valid is True


def test_hmac_requires_a_key_when_events_are_keyed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, hmac_key=b"secret")
    _append(ledger, 2)
    path = tmp_path / "task.jsonl"

    assert ledger.verify().valid is True
    keyed = Ledger.open(path, task_id="t1")
    report = keyed.verify()
    assert report.valid is False
    assert report.key_required is True
    assert keyed.verify(b"wrong").valid is False
    assert keyed.verify(b"secret").valid is True


def test_missing_anchor_still_verifies_the_chain(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger, 2)
    anchor = tmp_path / "task.jsonl.anchor"
    anchor.unlink()

    assert Ledger.open(tmp_path / "task.jsonl", task_id="t1").verify().valid is True


def test_read_only_view_cannot_append(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    view = ReadOnlyLedger(ledger)
    assert view.watermark() == 0
    with pytest.raises(TypeError):
        view.append(EventKind.USER_INPUT, EventSource.USER, {})


def test_projection_prefix_and_hash_are_deterministic(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger, 3)
    task = ledger.project("task", up_to_seq=2)
    assert task["last_seq"] == 2
    assert task["events"] == 2
    assert ledger.projection_hash("task") == ledger.projection_hash("task")
    assert ledger.projection_hash("task", up_to_seq=2) != ledger.projection_hash("task")


def test_in_memory_ledger_works_without_files() -> None:
    ledger = Ledger.in_memory("t1")
    ledger.append(EventKind.USER_INPUT, EventSource.USER, {"i": 0}, ts=TS)
    assert ledger.path is None
    assert ledger.watermark() == 1
    assert ledger.verify().valid is True
