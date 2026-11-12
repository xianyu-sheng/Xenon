"""Record a direct-chat turn in the kernel ledger.

Chat has no claims to verify; what it needs is a durable record that the turn
happened.  Writing it here keeps "the ledger is the only truth" true for every
turn without moving streaming chat into the kernel loop (that move would lose
token streaming and rewrite how the conversation prompt is assembled).

Ledgers live in a ``chat/`` subdirectory of the REPL ledger directory:
``kernel/session_history.py`` scans only the top level, so a greeting must not
push a real task out of the model's five-line window.

Writes are best-effort.  A ledger failure is logged at debug level and never
breaks the reply -- the answer is the product; the record is the evidence.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.store import Ledger, open_shared

logger = logging.getLogger(__name__)

#: Subdirectory under the REPL ledger directory (not scanned by session history).
CHAT_SUBDIR = "chat"

#: Phase names are the kernel's own; a chat turn walks the same shape so the
#: existing projections and replay read it without a special case.
_CREATED = "created"
_RUNNING = "running"
_DELIVERED = "delivered"
_FINALIZED = "finalized"
_FAILED = "failed"
_ABORTED = "aborted"


@dataclass(slots=True)
class ChatTurn:
    """An open conversational record; ``None`` when the ledger was unavailable."""

    ledger: Ledger
    task_id: str
    path: Path


def default_ledger_dir() -> Path:
    """The REPL's ledger directory, matching ``RuntimeAdapter``'s default.

    ``XENON_LEDGER_DIR`` exists so tests and sandboxed hosts never write chat
    records into the user's real ``~/.xenon``.
    """

    override = os.environ.get("XENON_LEDGER_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".xenon" / "ledger" / "repl"


def open_conversation_turn(
    ledger_dir: str | Path,
    *,
    objective: str,
    user_objective: str = "",
    workspace: str = "",
    session_id: str = "",
    model_ids: Sequence[str] = (),
) -> ChatTurn | None:
    """Start a chat ledger before generation, so a crash still leaves a record."""

    try:
        directory = Path(ledger_dir).expanduser() / CHAT_SUBDIR
        directory.mkdir(parents=True, exist_ok=True)
        task_id = f"chat-{uuid.uuid4().hex[:12]}"
        ledger = open_shared(directory / f"{task_id}.jsonl", task_id)
        ledger.append(
            EventKind.TASK_STARTED,
            EventSource.USER,
            {
                "objective": objective,
                # The user's own words, not the prompt the optimizer built --
                # session history shows this line to the model.
                "user_objective": user_objective or objective,
                "workspace": workspace,
                "session_id": session_id,
                "mode": "chat",
                "model_ids": list(model_ids),
            },
        )
        return ChatTurn(ledger=ledger, task_id=task_id, path=ledger.path or Path())
    except Exception:  # noqa: BLE001 — a missing record must not break the turn
        logger.debug("conversation ledger open failed", exc_info=True)
        return None


def finish_conversation_turn(
    turn: ChatTurn | None,
    *,
    answer: str = "",
    model_id: str = "",
    intent: str = "",
    intent_source: str = "",
    error: str = "",
    cancelled: bool = False,
    delegated: bool = False,
) -> Path | None:
    """Close the record with an explicit terminal phase; never leave it open."""

    if turn is None:
        return None
    try:
        ledger = turn.ledger
        if intent:
            ledger.append(
                EventKind.INTENT_CLASSIFIED,
                EventSource.RUNTIME,
                {
                    "intent": intent,
                    "source": intent_source or "direct",
                    "mode": "chat",
                    "model": model_id,
                },
            )
        ledger.append(
            EventKind.STATE_TRANSITION,
            EventSource.RUNTIME,
            {"from": _CREATED, "to": _RUNNING, "reason": "direct chat"},
        )
        if answer:
            ledger.append(
                EventKind.DELIVERED,
                EventSource.RUNTIME,
                {
                    "answer": answer,
                    "claims": [],
                    "verdicts": [],
                    "finalize": {"commit": None, "verified_paths": []},
                },
            )
            ledger.append(
                EventKind.STATE_TRANSITION,
                EventSource.RUNTIME,
                {"from": _RUNNING, "to": _DELIVERED, "reason": "answer"},
            )

        terminal, source = _FINALIZED, _DELIVERED
        reason = "chat turn"
        if delegated:
            terminal, source, reason = _ABORTED, _RUNNING, "delegated to tool path"
        elif cancelled:
            terminal, source, reason = _ABORTED, _RUNNING, "cancelled by user"
        elif error:
            terminal, source, reason = _FAILED, _RUNNING, error
        if source == _RUNNING:
            ledger.append(
                EventKind.STATE_TRANSITION,
                EventSource.RUNTIME,
                {"from": _RUNNING, "to": terminal, "reason": reason},
            )
        else:
            ledger.append(
                EventKind.STATE_TRANSITION,
                EventSource.RUNTIME,
                {"from": _DELIVERED, "to": terminal, "reason": reason},
            )
        ledger.append(
            EventKind.FINALIZED,
            EventSource.RUNTIME,
            {
                "commit": None,
                "evidence_status": "unverified",
                "error": error,
                "cancelled": cancelled,
                "delegated": delegated,
            },
        )
        return turn.path
    except Exception:  # noqa: BLE001 — closing a record never breaks the turn
        logger.debug("conversation ledger finish failed", exc_info=True)
        return None


__all__ = [
    "CHAT_SUBDIR",
    "ChatTurn",
    "default_ledger_dir",
    "finish_conversation_turn",
    "open_conversation_turn",
]
