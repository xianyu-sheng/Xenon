"""Shared writer and branch seals (design doc §7 / ledger ADR).

Two properties are pinned here:

* one file, one in-process ``Ledger`` object — two cached handles would compute
  the same ``seq`` and a divergent ``prev_hash``;
* a child chain is a *side chain* the parent seals by reference, so verifying
  the parent also verifies the branch and a tamper is reported as a fact.
"""

from __future__ import annotations

from pathlib import Path

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.store import Ledger, open_shared
from xenon.ledger.tree import verify_tree


def _append(ledger: Ledger, kind: EventKind = EventKind.STATE_TRANSITION) -> None:
    ledger.append(kind, EventSource.RUNTIME, {"note": "x"})


def test_one_file_gets_one_ledger_object(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"

    first = open_shared(path, "t1")
    second = open_shared(path, "t1")

    assert first is second


def test_a_second_handle_cannot_fork_the_chain(tmp_path: Path) -> None:
    """Simulates another writer: the first handle must notice and continue."""

    path = tmp_path / "l.jsonl"
    mine = open_shared(path, "t1")
    _append(mine)

    other = Ledger(path, "t1")  # a separate cached chain, like a second process
    _append(other)

    _append(mine)  # must reload first, or it would reuse seq 2 / the old tail

    reader = Ledger.open(path, "t1")
    assert reader.verify().valid
    assert [event.seq for event in reader.read()] == [1, 2, 3]


def _child(path: Path, parent_task_id: str) -> Ledger:
    child = Ledger(path, "child-1")
    child.append(
        EventKind.TASK_STARTED,
        EventSource.RUNTIME,
        {"parent_task_id": parent_task_id, "objective": "x"},
    )
    child.append(EventKind.FINALIZED, EventSource.RUNTIME, {"evidence_status": "verified"})
    return child


def _sealed_parent(tmp_path: Path) -> tuple[Path, Ledger]:
    parent_path = tmp_path / "parent.jsonl"
    parent = Ledger(parent_path, "t1")
    _append(parent)
    base_seq, base_hash = parent.watermark(), parent.last_hash()

    child = _child(tmp_path / "child-1.jsonl", "t1")
    parent.append(
        EventKind.CHILD_STARTED,
        EventSource.RUNTIME,
        {
            "child_id": "child-1",
            "objective": "x",
            "branch": {
                "task_id": "child-1",
                "ledger": "child-1.jsonl",
                "base_seq": base_seq,
                "base_hash": base_hash,
                "depth": 1,
            },
        },
    )
    parent.append(
        EventKind.CHILD_FINISHED,
        EventSource.RUNTIME,
        {
            "child_id": "child-1",
            "branch": {
                "task_id": "child-1",
                "ledger": "child-1.jsonl",
                "head_seq": child.watermark(),
                "head_hash": child.last_hash(),
                "event_count": len(child.read()),
                "sealed": True,
            },
        },
    )
    return parent_path, child


def test_a_sealed_branch_verifies(tmp_path: Path) -> None:
    parent_path, _child_ledger = _sealed_parent(tmp_path)

    integrity, reports = verify_tree(parent_path)

    assert integrity.valid
    assert [report.state for report in reports] == ["sealed"]


def test_a_tampered_child_chain_is_reported(tmp_path: Path) -> None:
    parent_path, child = _sealed_parent(tmp_path)
    # Someone appended to the child after the parent sealed its head.
    child.append(EventKind.DELIVERED, EventSource.RUNTIME, {"answer": "late"})

    integrity, reports = verify_tree(parent_path)

    assert integrity.valid  # the parent chain itself is untouched
    assert reports[0].state == "tampered"
    assert "sealed head" in reports[0].detail


def test_a_missing_child_ledger_is_reported_not_ignored(tmp_path: Path) -> None:
    parent_path, _ = _sealed_parent(tmp_path)
    (tmp_path / "child-1.jsonl").unlink()

    _integrity, reports = verify_tree(parent_path)

    assert reports[0].state == "missing"
