# Event source and projections

Status: **foundation landed; TaskState migration pending**.

## Why

Plan-Execute and TaskState currently keep several mutable views of the same
run: plan step status, tool tracker, delivery ledger, and the final answer.
When those views disagree, the engine can report a failed test as a completed
step.  The fix is not another gate; it is one durable event stream and
replayable projections.

## Current pieces

- `EvidenceLedger` is the append-only, hash-chained source of truth
  (`xenon/engine/evidence_runtime.py`).  Events have a sequence, phase, kind,
  source, payload, predecessor id and content hash.
- `EvidenceRuntime` is the facade over that ledger.
- `EvidenceSubscription` adds filtered pub/sub with optional replay.  A failing
  subscriber is isolated and cannot break the event log.
- Tool observations now carry structured `metadata` (exit code, changed /
  committed paths, result id), so projections do not have to parse prose.
- `EngineCallback` / `EventBus` are **ephemeral UI channels**.  They must not be
  the source of durable task state.

## Rules

1. A fact is written to `EvidenceLedger` once.
2. Projections only read events; they never mutate the ledger or another
   projection's state.
3. A projection must be replayable from a task boundary and idempotent by
   event id.
4. UI subscribers may drop events; durable projections may not.
5. A failed subscriber is logged and isolated.

## Target migration

1. Add a `TaskStateProjection` that consumes `tool_observation` events and
   replaces ad-hoc `update_from_calls` calls.
2. Add explicit plan/step event kinds, so plan status is a projection instead
   of a second mutable state machine.
3. Make plan and delivery statuses derived from the same event stream.
4. Let TUI/logging subscribe through `EventBus`; keep them out of state
   transitions.
5. Keep the existing `ToolExecutionTracker` as an in-memory execution trace,
   not as a second source of truth for delivery.

## Non-goals

- No external event-sourcing dependency is required for this step.
- No event payload should contain API keys, file contents, or other secrets.
- This document does not claim the migration is complete.
