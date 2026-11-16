"""Earlier turns of this session, one record each, from the ledgers themselves.

The ledger is already the record of every turn; copying summaries into each new
turn would make a second, weaker copy of it.  Instead the model is told that
earlier turns exist and may pull them on demand (design doc §3.2: "only prefix
+ this turn" — context comes back through a read-only query, not through
something we precompute).

Two directories hold a session's turns: kernel tasks live in the REPL ledger
directory, conversational turns in its ``chat/`` subdirectory (see
``xenon/ledger/conversation.py``).  Both are scanned: a follow-up such as
"对上面的第一点" may just as well point at an explanation that never ran a tool,
and a kernel prompt that could not see that turn answered "上一轮内容不存在" in
a real session.

Only ledgers for the same session count as "this session"; without one, the
workspace is the fallback boundary: one global ledger directory holds tasks
from every project the user opened, and a project's history would otherwise be
polluted by another's.

Reading is bounded: a task ledger is append-only and can be large, so the head
is read for the objective and the tail for the outcome.  A line that cannot be
parsed is skipped rather than guessed at.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from xenon.kernel.contracts import SessionTaskRecord
from xenon.ledger.events import EventKind

#: How many earlier tasks a session keeps for the model to read back.
DEFAULT_SESSION_HISTORY = 5

_HEAD_BYTES = 8 * 1024
_TAIL_BYTES = 64 * 1024
_FULL_READ_LIMIT = _HEAD_BYTES + _TAIL_BYTES
_CLIP = 160
#: How much of an earlier answer the structured projection keeps.  The prompt
#: renders a shorter excerpt; ``get_state what="task"`` renders up to this.
ANSWER_CLIP = 4000

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


def _ledger_dirs(directory: Path) -> list[Path]:
    """The directories that hold this session's ledgers.

    Kernel tasks sit in the ledger directory itself; direct-chat turns sit in
    its ``chat/`` subdirectory.  A caller whose own ledger is a chat record
    (``.../chat/chat-*.jsonl``) also needs the parent, or it could see neither
    the tasks nor its own session's earlier chats.
    """

    dirs = [directory]
    chat_dir = directory / "chat"
    dirs.append(chat_dir)
    if directory.name == "chat":
        dirs.append(directory.parent)
    return dirs


def _candidates(
    ledger_path: Path | str | None,
) -> tuple[Path, list[Path]]:
    """(current ledger, newest-first siblings across both dirs) or [].

    Scan errors degrade to an empty window: a prompt section is an aid, and a
    locked directory must not fail the turn.
    """

    if ledger_path is None:
        return Path(), []
    current = Path(ledger_path)
    # ``Path.resolve()`` per file is a filesystem round-trip on Windows; with
    # thousands of ledgers in the directory it dominated the whole lookup.
    # Normalised absolute paths identify the same file without touching disk.
    current_key = os.path.normcase(os.path.abspath(str(current)))
    candidates: list[Path] = []
    seen: set[str] = set()
    for directory in _ledger_dirs(current.parent):
        try:
            items = list(directory.glob("*.jsonl"))
        except OSError:
            continue
        for item in items:
            key = os.path.normcase(os.path.abspath(str(item)))
            if key == current_key or key in seen:
                continue
            seen.add(key)
            candidates.append(item)
    try:
        candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return current, []
    return current, candidates


def _belongs_to(
    path: Path,
    *,
    workspace: str,
    session_id: str,
) -> tuple[bool, str]:
    objective, task_workspace, task_session = _objective(path)
    if not objective:
        return False, ""
    if session_id:
        return task_session == session_id, objective
    if workspace and task_workspace and not _same_workspace(
        workspace, task_workspace
    ):
        return False, objective
    return True, objective


def recent_tasks(
    ledger_path: Path | str | None,
    *,
    workspace: str = "",
    session_id: str = "",
    limit: int = DEFAULT_SESSION_HISTORY,
) -> tuple[SessionTaskRecord, ...]:
    """The last ``limit`` tasks before the current one, newest first.

    ``session_id`` is the authoritative boundary: one conversation, not one
    directory.  Without it (a host that has no conversation) the workspace is
    the best available boundary, and a task from another workspace is skipped.

    Each record keeps a bounded answer excerpt (``ANSWER_CLIP``) plus the
    ledger path, so the model can read the full evidence through the ledger
    instead of being told a turn never happened.
    """

    _, candidates = _candidates(ledger_path)
    records: list[SessionTaskRecord] = []
    for candidate in candidates[:_SCAN_LIMIT]:
        if len(records) >= limit:
            break
        belongs, objective = _belongs_to(
            candidate, workspace=workspace, session_id=session_id
        )
        if not belongs:
            continue
        status, answer = _outcome(candidate)
        records.append(
            SessionTaskRecord(
                task_id=candidate.stem,
                objective=objective,
                status=status,
                answer=_clip_answer(answer),
                ledger=str(candidate),
            )
        )
    return tuple(records)


def _clip_answer(answer: str) -> str:
    answer = answer.strip()
    if len(answer) <= ANSWER_CLIP:
        return answer
    return answer[: ANSWER_CLIP - 1] + "…"


def task_record(
    ledger_path: Path | str | None,
    task_id: str,
) -> SessionTaskRecord | None:
    """One earlier task by id (its ledger's stem), or ``None``.

    The id is a file stem, never a path: a request that tries to walk out of
    the ledger directory finds nothing.
    """

    if ledger_path is None:
        return None
    task_id = task_id.strip()
    if not task_id or task_id != Path(task_id).name:
        return None
    candidate = None
    for directory in _ledger_dirs(Path(ledger_path).parent):
        probe = directory / f"{task_id}.jsonl"
        try:
            if probe.is_file():
                candidate = probe
                break
        except OSError:
            continue
    if candidate is None:
        return None
    objective, _, _ = _objective(candidate)
    if not objective:
        return None
    status, answer = _outcome(candidate)
    return SessionTaskRecord(
        task_id=task_id,
        objective=objective,
        status=status,
        answer=_clip_answer(answer),
        ledger=str(candidate),
    )


def record_line(record: SessionTaskRecord) -> str:
    """One clipped line for a projection record."""

    line = f"{record.task_id} · {_clip(record.objective)}"
    if record.status:
        line += f" → {record.status}"
    if record.answer:
        line += f" · {_clip(record.answer)}"
    return line


def recent_task_lines(
    ledger_path: Path | str | None,
    *,
    workspace: str = "",
    session_id: str = "",
    limit: int = DEFAULT_SESSION_HISTORY,
) -> tuple[str, ...]:
    """The last ``limit`` tasks, one clipped line each (newest first)."""

    return tuple(
        record_line(record)
        for record in recent_tasks(
            ledger_path, workspace=workspace, session_id=session_id, limit=limit
        )
    )


__all__ = [
    "ANSWER_CLIP",
    "DEFAULT_SESSION_HISTORY",
    "recent_task_lines",
    "recent_tasks",
    "record_line",
    "task_record",
]
