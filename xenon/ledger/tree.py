"""Verify a task ledger together with the child branches it sealed.

A delegated task writes its own ledger.  The parent never copies the child's
events into its own chain -- that would break its single linear hash chain and
make two writers own one file.  Instead it records a **branch point**
(``child_started.branch``: the parent's own seq/hash at that moment) and a
**seal** (``child_finished`` / ``child_interrupted``: the child's head, event
count, and whether the parent could verify the child's chain).

Verification is therefore a deterministic fold:

1. the parent chain verifies linearly, exactly as before;
2. each branch's file resolves and its chain verifies on its own;
3. the child's own ``task_started`` names this parent;
4. a sealed branch's recorded head still matches the file.

A missing or tampered branch is a *reported fact*, never an omission.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xenon.ledger.events import EventKind
from xenon.ledger.store import IntegrityReport, Ledger


@dataclass(frozen=True, slots=True)
class BranchReport:
    task_id: str
    ledger: str
    state: str  # sealed | pending | missing | tampered | unsealed
    detail: str = ""
    head_seq: int = 0
    head_hash: str = ""


def _branch_of(payload: Any) -> dict[str, Any]:
    branch = payload.get("branch") if isinstance(payload, dict) else None
    return branch if isinstance(branch, dict) else {}


def _resolve(base: Path, raw: str) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else base / path


def _parent_has(
    events: tuple[Any, ...], seq: int, digest: str
) -> bool:
    if not seq:
        return digest == ""
    for event in events:
        if event.seq == seq:
            return event.hash == digest
    return False


def _child_names_parent(child: Ledger, parent_task_id: str) -> bool:
    for event in child.read(EventKind.TASK_STARTED):
        payload = event.payload if isinstance(event.payload, dict) else {}
        return str(payload.get("parent_task_id") or "") == parent_task_id
    return False


def verify_tree(
    ledger_path: Path | str,
) -> tuple[IntegrityReport, tuple[BranchReport, ...]]:
    """Verify the parent chain and every branch it sealed."""

    parent = Ledger.open(ledger_path)
    integrity = parent.verify()
    events = parent.read()
    base = Path(ledger_path).parent

    starts: dict[str, dict[str, Any]] = {}
    seals: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        child_id = str(payload.get("child_id") or "")
        if not child_id:
            continue
        if event.kind is EventKind.CHILD_STARTED:
            starts[child_id] = _branch_of(payload)
        elif event.kind in (EventKind.CHILD_FINISHED, EventKind.CHILD_INTERRUPTED):
            seals[child_id] = _branch_of(payload)

    reports: list[BranchReport] = []
    for child_id, start in starts.items():
        seal = seals.get(child_id, {})
        raw = str(start.get("ledger") or seal.get("ledger") or "")
        path = _resolve(base, raw)
        if path is None or not path.exists():
            reports.append(
                BranchReport(
                    child_id,
                    raw,
                    "missing" if child_id in seals else "pending",
                    "child ledger not found",
                )
            )
            continue

        child = Ledger.open(path)
        child_report = child.verify()
        if not child_report.valid:
            reports.append(
                BranchReport(child_id, str(path), "tampered", child_report.reason[:200])
            )
            continue
        if not _child_names_parent(child, parent.task_id):
            reports.append(
                BranchReport(
                    child_id,
                    str(path),
                    "tampered",
                    "child ledger does not name this parent task",
                )
            )
            continue
        base_seq = int(start.get("base_seq") or 0)
        base_hash = str(start.get("base_hash") or "")
        if not _parent_has(events, base_seq, base_hash):
            reports.append(
                BranchReport(
                    child_id,
                    str(path),
                    "tampered",
                    "branch point does not match the parent chain",
                )
            )
            continue

        if child_id not in seals:
            reports.append(
                BranchReport(
                    child_id,
                    str(path),
                    "pending",
                    "started but never closed by the parent",
                    head_seq=child.watermark(),
                    head_hash=child.last_hash(),
                )
            )
            continue
        if not seal.get("sealed"):
            reports.append(
                BranchReport(
                    child_id,
                    str(path),
                    "unsealed",
                    str(seal.get("detail") or "the parent could not verify the child chain"),
                    head_seq=child.watermark(),
                    head_hash=child.last_hash(),
                )
            )
            continue
        recorded_head = str(seal.get("head_hash") or "")
        recorded_count = int(seal.get("event_count") or 0)
        if (
            recorded_head != child.last_hash()
            or recorded_count != len(child.read())
        ):
            reports.append(
                BranchReport(
                    child_id,
                    str(path),
                    "tampered",
                    "sealed head no longer matches the child ledger",
                )
            )
            continue
        reports.append(
            BranchReport(
                child_id,
                str(path),
                "sealed",
                "",
                head_seq=child.watermark(),
                head_hash=child.last_hash(),
            )
        )
    return integrity, tuple(reports)


__all__ = ["BranchReport", "verify_tree"]
