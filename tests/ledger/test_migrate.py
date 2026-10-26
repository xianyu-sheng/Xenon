"""Tests for legacy session migration.

Legacy records must be imported as audit-only events, never as claims/facts.
"""

from __future__ import annotations

import json
from pathlib import Path

from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.migrate import migrate_session_jsonl
from xenon.ledger.projections import project_evidence
from xenon.ledger.store import Ledger


def _write_legacy(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


def test_legacy_records_become_audit_only_events(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    _write_legacy(
        path,
        [
            {"id": "e1", "type": "turn/user", "at": 1.0, "text": "fix add"},
            {"id": "e2", "type": "tool/result", "at": 2.0, "tool": "mutate"},
        ],
    )
    ledger = Ledger.in_memory("t1")

    report = migrate_session_jsonl(path, ledger)

    assert report.imported == 2
    assert report.errors == ()
    legacy = ledger.read(EventKind.LEGACY_SESSION)
    assert [event.payload["legacy_type"] for event in legacy] == [
        "turn/user",
        "tool/result",
    ]
    assert legacy[0].source is EventSource.USER
    assert legacy[1].source is EventSource.TOOL

    evidence = project_evidence(ledger.read())
    assert evidence["claims"] == []
    assert evidence["receipts"] == []
    assert len(evidence["legacy_events"]) == 2


def test_rerun_skips_already_imported_ids(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    _write_legacy(path, [{"id": "e1", "type": "turn/user"}])
    ledger = Ledger.in_memory("t1")

    assert migrate_session_jsonl(path, ledger).imported == 1
    second = migrate_session_jsonl(path, ledger)
    assert second.imported == 0
    assert second.skipped == 1
    assert ledger.watermark() == 1


def test_bad_line_is_reported_without_blocking_good_lines(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps({"id": "e1", "type": "turn/user"}) + "\n"
        + "{not json\n"
        + json.dumps({"id": "e2", "type": "turn/assistant"}) + "\n",
        encoding="utf-8",
    )
    ledger = Ledger.in_memory("t1")

    report = migrate_session_jsonl(path, ledger)

    assert report.imported == 2
    assert len(report.errors) == 1
    assert "line 2" in report.errors[0]


def test_missing_legacy_log_is_reported(tmp_path: Path) -> None:
    report = migrate_session_jsonl(tmp_path / "missing.jsonl", Ledger.in_memory("t1"))
    assert report.imported == 0
    assert report.errors
