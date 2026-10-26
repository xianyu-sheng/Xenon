"""Best-effort migration of legacy session JSONL into ledger events.

Legacy session records are imported as ``legacy_session`` events, **not** as
claims or facts.  The old log mixed prose, tool summaries and authority events;
promoting any of that into the new evidence space would reintroduce the exact
"same event, many interpretations" problem.  This adapter preserves audit
history without granting it authority.

It is deliberately failure-tolerant: a corrupt legacy line is reported and
skipped, never allowed to block startup.  The legacy hash chain is preserved in
the payload for audit but is not re-interpreted as evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.store import Ledger, LedgerError

_LEGACY_FIELDS = frozenset({"id", "type", "at", "h", "prev", "hk"})


@dataclass(frozen=True, slots=True)
class MigrationReport:
    imported: int = 0
    skipped: int = 0
    errors: tuple[str, ...] = ()


def _source_for(legacy_type: str) -> EventSource:
    if legacy_type.startswith("turn/user"):
        return EventSource.USER
    if legacy_type.startswith("turn/"):
        return EventSource.LLM
    if legacy_type.startswith("tool/"):
        return EventSource.TOOL
    if legacy_type.startswith("gate/"):
        return EventSource.GATE
    return EventSource.RUNTIME


def migrate_session_jsonl(
    path: Path | str,
    ledger: Ledger,
    *,
    source: EventSource | None = None,
) -> MigrationReport:
    """Import legacy records once; reruns skip already-imported legacy IDs."""

    log_path = Path(path)
    if not log_path.exists():
        return MigrationReport(errors=(f"legacy log not found: {log_path}",))

    imported = 0
    skipped = 0
    errors: list[str] = []
    seen = {
        str(event.payload.get("legacy_id") or "")
        for event in ledger.read(EventKind.LEGACY_SESSION)
    }

    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return MigrationReport(errors=(f"cannot read legacy log: {exc}",))

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_number}: {exc.msg}")
            continue
        if not isinstance(record, dict):
            errors.append(f"line {line_number}: not a JSON object")
            continue

        legacy_id = str(record.get("id") or f"line-{line_number}")
        if legacy_id in seen:
            skipped += 1
            continue
        legacy_type = str(record.get("type") or "unknown")
        payload = {
            "legacy_id": legacy_id,
            "legacy_type": legacy_type,
            "legacy_at": record.get("at"),
            "legacy_prev": record.get("prev"),
            "legacy_hash": record.get("h"),
            "data": {
                key: value
                for key, value in record.items()
                if key not in _LEGACY_FIELDS
            },
        }
        try:
            ledger.append(
                EventKind.LEGACY_SESSION,
                source or _source_for(legacy_type),
                payload,
            )
        except LedgerError as exc:
            errors.append(f"line {line_number}: {exc}")
            break
        seen.add(legacy_id)
        imported += 1

    return MigrationReport(
        imported=imported,
        skipped=skipped,
        errors=tuple(errors),
    )
