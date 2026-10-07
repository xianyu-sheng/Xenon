"""Additive session event log.

The authoritative conversation/session formats are untouched: this log is a
best-effort sidecar (JSONL) that records durable facts for replay,
projections and read-only retrieval.  Every write is failure-tolerant — a
read-only home, a full disk or a corrupt line must never break a turn.

Fact events (safe to index for retrieval):

* ``turn/user``      — user request + resolved contract summary
* ``turn/assistant`` — assistant answer preview
* ``tool/result``    — tool receipt (tool, success, canonical paths)
* ``goal/opened`` / ``goal/paused`` / ``goal/closed``
* ``plan_mode/set``  — logged plan-mode state

Authority events (never indexed, never retrieved):

* ``gate/opened`` / ``gate/resolved`` — pending approvals are facts about a
  specific past turn, but letting retrieval resurface them would resurrect
  stale authority.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

FACT_EVENT_TYPES = frozenset(
    {
        "turn/user",
        "turn/assistant",
        "tool/result",
        "goal/opened",
        "goal/paused",
        "goal/closed",
    }
)
AUTHORITY_EVENT_TYPES = frozenset({"gate/opened", "gate/resolved"})
KNOWN_EVENT_TYPES = FACT_EVENT_TYPES | AUTHORITY_EVENT_TYPES | {"plan_mode/set"}


def session_events_dir() -> Path:
    override = os.environ.get("XENON_SESSION_EVENTS_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".xenon" / "sessions" / "events"


class SessionEventLog:
    """Append-only JSONL writer/reader for one session."""

    def __init__(
        self,
        session_id: str | None = None,
        *,
        directory: str | os.PathLike[str] | None = None,
        enabled: bool = True,
    ) -> None:
        self.session_id = session_id or self._new_session_id()
        self.directory = Path(directory) if directory else session_events_dir()
        self.enabled = enabled and os.environ.get("XENON_SESSION_EVENTS") != "0"

    @staticmethod
    def _new_session_id() -> str:
        return time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}-{uuid.uuid4().hex[:6]}"

    @property
    def path(self) -> Path:
        return self.directory / f"{self.session_id}.jsonl"

    def append(self, event_type: str, **data: object) -> str | None:
        """Append one event; returns its id, or ``None`` when best-effort IO fails."""

        if not self.enabled:
            return None
        event_id = uuid.uuid4().hex[:12]
        record = {
            "id": event_id,
            "type": event_type,
            "at": time.time(),
            **data,
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # pragma: no cover - platform dependent
            logger.debug("会话事件写入失败（已忽略）: %s", exc)
            return None
        return event_id

    def read(self) -> list[dict]:
        """Read all parseable events; corrupt lines are skipped."""

        if not self.path.exists():
            return []
        events: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict) and record.get("type"):
                        events.append(record)
        except OSError as exc:  # pragma: no cover - platform dependent
            logger.debug("会话事件读取失败（已忽略）: %s", exc)
        return events
