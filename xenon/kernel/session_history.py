"""Earlier tasks of this session, one line each, from the ledgers themselves.

The ledger is already the record of every task; copying summaries into each new
turn would make a second, weaker copy of it.  Instead the model is told that
earlier tasks exist and may pull the last few lines on demand (design doc §3.2:
"only prefix + this turn" — context comes back through a read-only query, not
through something we precompute).

Only ledgers in the same directory and for the same workspace count as "this
session": one global ledger directory holds tasks from every project the user
opened, and a project's history would otherwise be polluted by another's.

Reading is bounded: a task ledger is append-only and can be large, so the head
is read for the objective and the tail for the outcome.  A line that cannot be
parsed is skipped rather than guessed at.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from xenon.ledger.events import EventKind

#: How many earlier tasks a session keeps for the model to read back.
DEFAULT_SESSION_HISTORY = 5

_HEAD_BYTES = 8 * 1024
_TAIL_BYTES = 64 * 1024
_FULL_READ_LIMIT = _HEAD_BYTES + _TAIL_BYTES
_CLIP = 160

#: How many of the newest ledgers a lookup may open.  The real REPL directory
#: holds thousands of tasks from every project; scanning all of them cost ~1.6s
#: per run, and a session's own tasks are always among the most recent.  The
#: bound keeps the query O(1) in directory size instead of O(N).
_SCAN_LIMIT = 40


def _records(path: Path, *, head: bool, tail: bool) -> list[dict]:
    """Parse the head and/or tail records of a JSONL ledger, tolerating junk."""

    try:
        size = path.stat().st_size
    except OSError:
        return []
    blocks: list[str] = []
    try:
        with path.open("rb") as stream:
            if size <= _FULL_READ_LIMIT:
                blocks.append(stream.read().decode("utf-8", errors="replace"))
            else:
                if head:
                    blocks.append(
                        stream.read(_HEAD_BYTES).decode("utf-8", errors="replace")
                    )
                if tail:
                    stream.seek(max(0, size - _TAIL_BYTES))
                    blocks.append(stream.read().decode("utf-8", errors="replace"))
    except OSError:
        return []
    out: list[dict] = []
    for block in blocks:
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a split or truncated line is not a claim
            if isinstance(record, dict):
                out.append(record)
    return out


def _objective(path: Path) -> tuple[str, str, str]:
    """(objective, workspace, session_id) from the task's opening record."""

    for record in _records(path, head=True, tail=False):
        if record.get("kind") != EventKind.TASK_STARTED.value:
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            break
        objective = str(
            payload.get("user_objective") or payload.get("objective") or ""
        ).strip()
        return (
            objective,
            str(payload.get("workspace") or ""),
            str(payload.get("session_id") or ""),
        )
    return "", "", ""


def _outcome(path: Path) -> tuple[str, str]:
    """(evidence_status, answer) from the task's closing records."""

    status = ""
    answer = ""
    for record in _records(path, head=False, tail=True):
        kind = record.get("kind")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if kind == EventKind.FINALIZED.value:
            status = str(payload.get("evidence_status") or "") or status
        elif kind == EventKind.DELIVERED.value and not answer:
            answer = str(payload.get("answer") or "").strip()
    return status, answer


def _clip(text: str, limit: int = _CLIP) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _same_workspace(left: str, right: str) -> bool:
    try:
        return os.path.normcase(os.path.normpath(left)) == os.path.normcase(
            os.path.normpath(right)
        )
    except (TypeError, ValueError):
        return left == right


def recent_task_lines(
    ledger_path: Path | str | None,
    *,
    workspace: str = "",
    session_id: str = "",
    limit: int = DEFAULT_SESSION_HISTORY,
) -> tuple[str, ...]:
    """The last ``limit`` tasks before the current one, newest first.

    ``session_id`` is the authoritative boundary: one conversation, not one
    directory.  Without it (a host that has no conversation) the workspace is
    the best available boundary, and a task from another workspace is skipped.
    """

    if ledger_path is None:
        return ()
    current = Path(ledger_path)
    directory = current.parent
    # ``Path.resolve()`` per file is a filesystem round-trip on Windows; with
    # thousands of ledgers in the directory it dominated the whole lookup.
    # Normalised absolute paths identify the same file without touching disk.
    current_key = os.path.normcase(os.path.abspath(str(current)))
    try:
        candidates = [
            item
            for item in directory.glob("*.jsonl")
            if os.path.normcase(os.path.abspath(str(item))) != current_key
        ]
        candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return ()

    lines: list[str] = []
    for candidate in candidates[:_SCAN_LIMIT]:
        if len(lines) >= limit:
            break
        objective, task_workspace, task_session = _objective(candidate)
        if not objective:
            continue
        if session_id:
            if task_session != session_id:
                continue
        elif workspace and task_workspace and not _same_workspace(
            workspace, task_workspace
        ):
            continue
        status, answer = _outcome(candidate)
        line = f"{candidate.stem} · {_clip(objective)}"
        if status:
            line += f" → {status}"
        if answer:
            line += f" · {_clip(answer)}"
        lines.append(line)
    return tuple(lines)


__all__ = ["DEFAULT_SESSION_HISTORY", "recent_task_lines"]
