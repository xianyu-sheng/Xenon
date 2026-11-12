"""Retention: archive old child ledgers.  Never delete evidence.

Design rules (doc §13.2 #15): retention is opt-in, scoped to the child ledger
directory, and moves files rather than removing them.  The task ledgers -- the
evidence chains a reviewer reads -- are never touched, and a file named by a
branch seal or by an open ``child_started`` is never moved out from under its
parent.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger

DEFAULT_CHILDREN_DAYS = 30


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Opt-in by default: a fresh install must not move anything."""

    enabled: bool = False
    children_days: int = DEFAULT_CHILDREN_DAYS
    max_total_mb: float = 0.0


def policy_from_config(config: object | None) -> RetentionPolicy:
    """Map the merged ``SystemConfig.retention`` section onto a policy.

    Env vars are already folded in by :func:`xenon.repl.system_config.get_config`
    (env > config.yaml > defaults), so this module never reads the environment
    itself -- one configuration path, not two.
    """

    if config is None:
        return RetentionPolicy()
    return RetentionPolicy(
        enabled=bool(getattr(config, "enabled", False)),
        children_days=max(1, int(getattr(config, "children_days", DEFAULT_CHILDREN_DAYS))),
        max_total_mb=max(0.0, float(getattr(config, "max_total_mb", 0.0))),
    )


def _sealed_child_ids(ledger_dir: Path) -> set[str]:
    """Child ids whose branch a parent sealed or still holds open."""

    protected: set[str] = set()
    for path in ledger_dir.glob("*.jsonl"):
        try:
            ledger = Ledger.open(path)
        except Exception:  # noqa: BLE001 — one bad ledger must not stop retention
            continue
        for event in ledger.read():
            payload = event.payload if isinstance(event.payload, dict) else {}
            child_id = str(payload.get("child_id") or "")
            if not child_id:
                continue
            if event.kind in (
                EventKind.CHILD_STARTED,
                EventKind.CHILD_FINISHED,
                EventKind.CHILD_INTERRUPTED,
            ):
                protected.add(child_id)
    return protected


def _archive(path: Path, directory: Path, stamp: str) -> Path | None:
    """Move one ledger into ``archive/<stamp>/``; None when the move fails."""

    target_dir = directory / "archive" / stamp
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        shutil.move(str(path), str(target))
    except OSError:
        return None
    return target


def _total_bytes(children: Path) -> int:
    total = 0
    for path in children.glob("*.jsonl"):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def enforce(ledger_dir: str | Path, policy: RetentionPolicy) -> list[Path]:
    """Move expired child ledgers into ``ledger/archive/<date>/``.

    Two independent reasons to archive, both bounded by the same rule that a
    branch a parent sealed or still holds open is never moved:

    1. **age** -- older than ``children_days``;
    2. **size** -- ``max_total_mb`` exceeded, oldest first until under budget.

    Returns the moved paths so a caller can say what happened.  Nothing is
    removed, and the main ledger directory is never pruned.
    """

    if not policy.enabled:
        return []
    directory = Path(ledger_dir).expanduser()
    children = directory / "children"
    if not children.is_dir():
        return []
    protected = _sealed_child_ids(directory)
    stamp = time.strftime("%Y%m%d", time.localtime())
    cutoff = time.time() - policy.children_days * 86400
    moved: list[Path] = []

    for path in sorted(children.glob("*.jsonl")):
        if path.stem in protected:
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        target = _archive(path, directory, stamp)
        if target is not None:
            moved.append(target)

    limit = policy.max_total_mb * 1024 * 1024
    if limit > 0:
        remaining = []
        for path in children.glob("*.jsonl"):
            if path.stem in protected:
                continue
            try:
                remaining.append((path.stat().st_mtime, path))
            except OSError:
                continue
        total = _total_bytes(children)
        for _mtime, path in sorted(remaining, key=lambda item: item[0]):
            if total <= limit:
                break
            try:
                size = path.stat().st_size
            except OSError:
                continue
            target = _archive(path, directory, stamp)
            if target is not None:
                moved.append(target)
                total -= size
    return moved

