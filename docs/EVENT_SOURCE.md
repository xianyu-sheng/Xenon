# Event source and projections

Status: **EventLedger subscription + TaskState/plan projections landed; remaining ad-hoc fallbacks marked below**.

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
- `TaskStateProjection` consumes `tool_observation`, `plan`,
  `plan_step_started` and `plan_step_finished` events, replays from a
  `task_fact` boundary, and deduplicates tool observations by `result_id`.
  `run_with_delivery` uses it when a boundary exists and falls back to the
  tracker otherwise.
- REPL auto-save / `/save` persist a verified ledger snapshot; `/load` and
  `/resume` rebind it before execution, so replay survives a restart.
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

1. Remove the remaining ad-hoc `update_from_calls` / direct `_sync_task_*`
   writes when a task boundary exists.
2. Let TUI/logging subscribe through `EventBus`; keep them out of state
   transitions.
3. Keep the existing `ToolExecutionTracker` as an in-memory execution trace,
   not as a second source of truth for delivery.
4. Add replay tests for a resumed task and a re-planned task.

## Non-goals

- No external event-sourcing dependency is required for this step.
- No event payload should contain API keys, file contents, or other secrets.
- This document does not claim the migration is complete.
