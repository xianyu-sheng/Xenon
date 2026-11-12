"""Crash recovery and retention: repair is visible, archiving is opt-in."""

from __future__ import annotations

import os
import time
from pathlib import Path

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.retention import RetentionPolicy, enforce
from xenon.ledger.store import Ledger


def _old(path: Path, days: int = 90) -> None:
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


def test_a_half_written_tail_is_quarantined_and_recorded(tmp_path: Path) -> None:
    path = tmp_path / "task.jsonl"
    ledger = Ledger(path, "t1")
    ledger.append(EventKind.STATE_TRANSITION, EventSource.RUNTIME, {"from": "a", "to": "b"})
    path.write_bytes(path.read_bytes() + b'{"seq":2,"kind":"half')

    broken = Ledger.open(path, "t1")
    assert broken.truncated_tail

    from xenon.harness.runner import _repair_incomplete_tail

    _repair_incomplete_tail(broken)

    repaired = Ledger.open(path, "t1")
    assert repaired.verify().valid
    kinds = [event.kind.value for event in repaired.read()]
    assert "ledger_repaired" in kinds
    assert "debt_opened" in kinds
    assert list(tmp_path.glob("*.corrupt-*"))


def test_retention_is_opt_in(tmp_path: Path) -> None:
    child = tmp_path / "children" / "c.jsonl"
    child.parent.mkdir(parents=True)
    Ledger(child, "c").append(EventKind.FINALIZED, EventSource.RUNTIME, {})
    _old(child)

    assert enforce(tmp_path, RetentionPolicy(enabled=False)) == []
    assert child.exists()


def test_an_expired_child_is_archived_not_deleted(tmp_path: Path) -> None:
    child = tmp_path / "children" / "c.jsonl"
    child.parent.mkdir(parents=True)
    Ledger(child, "c").append(EventKind.FINALIZED, EventSource.RUNTIME, {})
    _old(child)

    moved = enforce(tmp_path, RetentionPolicy(enabled=True, children_days=30))

    assert [path.name for path in moved] == ["c.jsonl"]
    assert moved[0].exists()
    assert moved[0].parent.parent.name == "archive"
    assert not child.exists()


def test_a_branch_named_by_a_parent_is_never_archived(tmp_path: Path) -> None:
    parent = Ledger(tmp_path / "p.jsonl", "p")
    parent.append(
        EventKind.CHILD_STARTED,
        EventSource.RUNTIME,
        {"child_id": "c1", "branch": {"task_id": "c1", "ledger": "children/c1.jsonl"}},
    )
    child = tmp_path / "children" / "c1.jsonl"
    child.parent.mkdir(parents=True)
    Ledger(child, "c1").append(EventKind.FINALIZED, EventSource.RUNTIME, {})
    _old(child)

    assert enforce(tmp_path, RetentionPolicy(enabled=True, children_days=30)) == []
    assert child.exists()


def test_a_size_budget_archives_the_oldest_child_first(tmp_path: Path) -> None:
    """``max_total_mb`` is a real knob: over budget, oldest branches go first."""

    children = tmp_path / "children"
    children.mkdir(parents=True)
    oldest = children / "a.jsonl"
    for path, task, days in (
        (oldest, "a", 30),
        (children / "b.jsonl", "b", 20),
        (children / "c.jsonl", "c", 10),
    ):
        Ledger(path, task).append(EventKind.FINALIZED, EventSource.RUNTIME, {})
        _old(path, days)

    total = sum(path.stat().st_size for path in children.glob("*.jsonl"))
    policy = RetentionPolicy(
        enabled=True,
        children_days=3650,  # age is not the trigger in this test
        max_total_mb=(total - 1) / (1024 * 1024),
    )

    moved = enforce(tmp_path, policy)

    assert [path.name for path in moved] == ["a.jsonl"]
    assert not oldest.exists()
    assert (tmp_path / "children" / "b.jsonl").exists()


def test_equal_timestamps_do_not_break_size_archiving(tmp_path: Path) -> None:
    """Ties on mtime must not make the sort compare ``Path`` objects."""

    children = tmp_path / "children"
    children.mkdir(parents=True)
    for name in ("a", "b"):
        path = children / f"{name}.jsonl"
        Ledger(path, name).append(EventKind.FINALIZED, EventSource.RUNTIME, {})
        _old(path, 90)

    total = sum(path.stat().st_size for path in children.glob("*.jsonl"))
    policy = RetentionPolicy(
        enabled=True,
        children_days=3650,
        max_total_mb=(total - 1) / (1024 * 1024),
    )

    assert len(enforce(tmp_path, policy)) == 1


def test_legacy_sessions_are_imported_once_into_their_own_ledgers(
    tmp_path: Path,
) -> None:
    import json

    from xenon.ledger.migrate import migrate_legacy_sessions

    sessions = tmp_path / "sessions"
    events = sessions / "events"
    events.mkdir(parents=True)
    (events / "s1.jsonl").write_text(
        json.dumps({"id": "e1", "type": "turn/user", "at": 1, "message": "hi"}) + "\n",
        encoding="utf-8",
    )
    ledger_dir = tmp_path / "ledger"

    first = migrate_legacy_sessions(sessions, ledger_dir)
    second = migrate_legacy_sessions(sessions, ledger_dir)

    assert (first.logs, first.imported) == (1, 1)
    assert second.imported == 0  # idempotent: the legacy id is already recorded
    target = ledger_dir / "legacy" / "s1.jsonl"
    assert target.exists()
    assert Ledger.open(target).verify().valid
    # It is audit history in its own chain, never mixed into a task ledger.
    assert not (ledger_dir / "s1.jsonl").exists()


def test_the_startup_path_does_not_sweep_the_legacy_session_store() -> None:
    """Importing legacy logs is an explicit command, never a startup hook.

    The legacy store is still written by /resume and /evidence; sweeping it on
    every start copies live data forever.  Measured on a real install: 25k
    files, ~165s per start.
    """

    import inspect

    from xenon.repl.repl import start_repl

    assert "migrate_legacy_sessions" not in inspect.getsource(start_repl)


def test_retention_policy_is_mapped_from_the_one_config_object() -> None:
    from types import SimpleNamespace

    from xenon.ledger.retention import policy_from_config

    policy = policy_from_config(
        SimpleNamespace(enabled=True, children_days=0, max_total_mb=-5)
    )
    assert policy.enabled is True
    assert policy.children_days >= 1  # clamped: 0 days would archive everything
    assert policy.max_total_mb == 0.0

    assert policy_from_config(None).enabled is False


def _started_call(tmp_path: Path, *, with_receipt: bool = False) -> Ledger:
    from xenon.kernel.contracts import ReceiptStatus, ToolReceipt
    from xenon.ledger.events import receipt_event_payload

    ledger = Ledger(tmp_path / "t.jsonl", "t")
    ledger.append(
        EventKind.TASK_STARTED, EventSource.USER, {"objective": "x"}
    )
    ledger.append(
        EventKind.TOOL_STARTED,
        EventSource.RUNTIME,
        {"call_id": "call-1", "tool": "test", "params": {}},
    )
    if with_receipt:
        receipt = ToolReceipt(
            receipt_id="r1",
            call_id="call-1",
            task_id="t",
            tool="test",
            status=ReceiptStatus.OK,
        )
        ledger.append(
            EventKind.TOOL_RECEIPT,
            EventSource.TOOL,
            receipt_event_payload(receipt),
        )
    return ledger


def test_a_start_without_a_receipt_becomes_an_unknown_outcome(
    tmp_path: Path,
) -> None:
    from xenon.harness.runner import _reconcile_interrupted_calls

    ledger = _started_call(tmp_path)

    _reconcile_interrupted_calls(ledger)

    reopened = Ledger.open(tmp_path / "t.jsonl")
    assert reopened.verify().valid
    kinds = [event.kind for event in reopened.read()]
    assert EventKind.TOOL_INTERRUPTED in kinds
    assert EventKind.DEBT_OPENED in kinds
    interrupted = reopened.read(EventKind.TOOL_INTERRUPTED)[0].payload
    assert interrupted["call_id"] == "call-1"
    assert interrupted["outcome"] == "unknown"
    debt = reopened.read(EventKind.DEBT_OPENED)[0].payload
    assert debt["obligation_id"] == "call:call-1"


def test_a_start_with_a_receipt_is_left_alone(tmp_path: Path) -> None:
    from xenon.harness.runner import _reconcile_interrupted_calls

    ledger = _started_call(tmp_path, with_receipt=True)
    before = len(ledger.read())

    _reconcile_interrupted_calls(ledger)

    assert len(ledger.read()) == before


def test_reconciliation_is_idempotent(tmp_path: Path) -> None:
    from xenon.harness.runner import _reconcile_interrupted_calls

    ledger = _started_call(tmp_path)
    _reconcile_interrupted_calls(ledger)
    after_first = len(ledger.read())

    _reconcile_interrupted_calls(ledger)

    assert len(ledger.read()) == after_first

