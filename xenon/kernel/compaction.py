"""Deterministic compaction of the working set.

The kernel never compresses history with a model: a fold here is a pure
function of the ledger.  Label events (debt, failed receipts, verdicts) are
pinned by the view; everything else competes for the remaining character
budget.  Whatever loses the competition is folded into one digest entry, so
the model still learns *that* a tool ran, with what status, at which seq —
dropping events silently is exactly the failure mode this module exists to
avoid.

Kept separate from ``view/base.py`` on purpose: view assembly is about shape,
this module is about sizing policy.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = [
    "ELIDED_KIND",
    "fold_dropped",
    "elided_line",
    "elided_room",
]

ELIDED_KIND = "elided"

# Reserved characters guarantee a compaction record survives even when the
# pinned label events already consume the whole budget.
ELIDED_MIN_CHARS = 240
ELIDED_MAX_LINES = 12
ELIDED_NOTE_SEQS = 6


def elided_line(item: Mapping[str, Any]) -> str:
    """One deterministic line standing in for a dropped event."""

    parts = [str(item.get("kind") or "event")]
    status = item.get("status")
    if status:
        parts.append(f"status={_clip(status, 60)}")
    tool = item.get("tool")
    if tool:
        parts.append(f"tool={_clip(tool, 60)}")
    step_id = item.get("step_id")
    if step_id is not None:
        parts.append(f"step={step_id}")
    return f"seq={item.get('seq')} " + " ".join(parts)


def elided_room(max_chars: int, used: int) -> int:
    """Characters the digest may claim: a reserved slice, never negative."""

    return max(ELIDED_MIN_CHARS, max_chars // 6, max_chars - used)


def fold_dropped(
    dropped: Sequence[Mapping[str, Any]],
    *,
    max_chars: int,
    used: int,
    size_of,
) -> dict[str, Any] | None:
    """Build the digest entry for ``dropped`` (``None`` when nothing dropped).

    ``size_of`` measures a candidate entry exactly the way the caller measures
    the rest of the working set, so the budget check uses one yardstick.
    """

    if not dropped:
        return None
    room = elided_room(max_chars, used)
    seqs = [item["seq"] for item in dropped]
    lines = [elided_line(item) for item in dropped][-ELIDED_MAX_LINES:]
    entry: dict[str, Any] = {
        "seq": seqs[0],
        "kind": ELIDED_KIND,
        "count": len(dropped),
        "seqs": seqs,
        "lines": lines,
    }
    while lines and size_of(entry) > room:
        lines = lines[1:]
        entry["lines"] = lines
    if not lines or size_of(entry) > room:
        # Even one line does not fit: keep the fact, drop the detail.
        entry = {
            "seq": seqs[0],
            "kind": ELIDED_KIND,
            "count": len(dropped),
            "seqs": seqs[-ELIDED_NOTE_SEQS:],
        }
    return entry


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"
