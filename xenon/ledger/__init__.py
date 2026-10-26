"""Append-only execution ledger (#194).

The ledger stores typed events, not prose.  Receipts are atomic events; claims
and hypotheses have separate ID spaces.  Projections are pure functions over an
event prefix.
"""

from xenon.ledger.events import (
    SCHEMA_VERSION,
    Event,
    EventKind,
    EventSource,
    canonical_json,
    claim_id,
    effect_from_payload,
    effect_to_payload,
    fact_id,
    hypothesis_id,
    receipt_event_payload,
    receipt_fact_ids,
    receipt_from_payload,
    receipt_to_payload,
    utc_now,
)
from xenon.ledger.migrate import MigrationReport, migrate_session_jsonl
from xenon.ledger.projections import (
    PROJECTIONS,
    project,
    project_all,
    project_audit,
    project_evidence,
    project_plan,
    project_task_state,
)
from xenon.ledger.store import (
    ANCHOR_SUFFIX,
    IntegrityReport,
    Ledger,
    LedgerError,
    ReadOnlyLedger,
    read_events,
)

__all__ = [
    "ANCHOR_SUFFIX",
    "Event",
    "EventKind",
    "EventSource",
    "IntegrityReport",
    "Ledger",
    "LedgerError",
    "MigrationReport",
    "PROJECTIONS",
    "ReadOnlyLedger",
    "SCHEMA_VERSION",
    "canonical_json",
    "claim_id",
    "effect_from_payload",
    "effect_to_payload",
    "fact_id",
    "hypothesis_id",
    "migrate_session_jsonl",
    "project",
    "project_all",
    "project_audit",
    "project_evidence",
    "project_plan",
    "project_task_state",
    "read_events",
    "receipt_event_payload",
    "receipt_fact_ids",
    "receipt_from_payload",
    "receipt_to_payload",
    "utc_now",
]
