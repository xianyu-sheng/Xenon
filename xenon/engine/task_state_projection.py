"""Replayable projection: durable tool observations -> TaskState.

TaskState is execution state, not evidence.  This projector is the bridge from
the hash-chained EvidenceLedger to TaskState: it applies each tool observation
once, in ledger order, and writes the resulting snapshot back to AgentContext.
"""

from __future__ import annotations

from typing import Any

from xenon.engine.evidence_runtime import (
    EventKind,
    EvidenceEvent,
    EvidenceLedger,
    EvidenceSubscription,
)
from xenon.engine.task_state import TaskState


class TaskStateProjection:
    """Apply plan and tool events to one TaskState."""

    def __init__(
        self,
        state: TaskState,
        context: Any | None = None,
        on_change: Any | None = None,
    ) -> None:
        self.state = state
        self.context = context
        self.on_change = on_change
        self.subscription: EvidenceSubscription | None = None
        self.ledger: EvidenceLedger | None = None

    def __call__(self, event: EvidenceEvent) -> None:
        if not self.state.active:
            return
        payload = event.payload or {}
        task_id = str(payload.get("task_id") or "")
        if task_id and self.state.task_id and task_id != self.state.task_id:
            return

        if event.kind == EventKind.PLAN.value:
            steps = payload.get("steps")
            if isinstance(steps, list):
                self.state.set_plan(
                    steps,
                    version=int(payload.get("version") or self.state.plan_version),
                    reason=str(payload.get("reason") or ""),
                )
                self._publish(notify=True)
            return

        if event.kind == EventKind.PLAN_STEP_STARTED.value:
            step_id = payload.get("step_id")
            self.state.upsert_step(
                {
                    "id": step_id,
                    "task": payload.get("task"),
                    "kind": payload.get("kind"),
                    "requires_write": payload.get("requires_write"),
                    "verify_cmd": payload.get("verify_cmd"),
                    "parent_step_id": payload.get("parent_step_id"),
                    "origin": payload.get("origin") or "plan",
                    "plan_version": payload.get("plan_version"),
                    "attempt": payload.get("attempt"),
                    "status": "running",
                },
                status="running",
            )
            self.state.set_step_status(step_id, "running")
            self._publish(notify=True)
            return

        if event.kind == EventKind.PLAN_STEP_FINISHED.value:
            raw_status = str(payload.get("status") or "")
            if raw_status == "skipped":
                status = "skipped"
            elif payload.get("success") is True or raw_status == "ok":
                status = "completed"
            else:
                status = "failed"
            step_id = payload.get("step_id")
            details = {
                "attempts": payload.get("attempts"),
                "last_error": payload.get("error"),
                "last_summary": payload.get("summary"),
            }
            if not self.state.set_step_status(step_id, status, **details):
                self.state.upsert_step(
                    {
                        "id": step_id,
                        "task": payload.get("task"),
                        "kind": payload.get("kind"),
                        "requires_write": payload.get("requires_write"),
                        "verify_cmd": payload.get("verify_cmd"),
                        "parent_step_id": payload.get("parent_step_id"),
                        "origin": payload.get("origin") or "remediation",
                        "plan_version": payload.get("plan_version"),
                        "status": status,
                        **details,
                    },
                    status=status,
                )
            self._publish(notify=True)
            return

        if event.kind != EventKind.TOOL_OBSERVATION.value:
            return
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        call_id = str(metadata.get("result_id") or event.event_id)
        self.state.record_tool(
            str(payload.get("tool") or ""),
            payload.get("params") if isinstance(payload.get("params"), dict) else {},
            success=bool(payload.get("success")),
            result=str(payload.get("summary") or ""),
            metadata=metadata,
            call_id=call_id,
        )
        self._publish()

    def _publish(self, *, notify: bool = False) -> None:
        if self.context is not None and hasattr(self.context, "set"):
            self.context.set("_task_state", self.state.to_dict())
        if notify and self.on_change is not None:
            try:
                self.on_change(self.state)
            except Exception:  # noqa: BLE001 — projection must not break execution
                pass

    def attach(
        self,
        ledger: EvidenceLedger,
        *,
        start_sequence: int = 0,
        replay: bool = True,
    ) -> "TaskStateProjection":
        """Attach to the ledger, optionally replaying from a task boundary."""
        self.ledger = ledger
        self.subscription = ledger.subscribe(self, replay=False)
        if replay:
            for event in list(ledger.events):
                if event.sequence >= int(start_sequence):
                    self(event)
        return self

    def close(self) -> None:
        if self.subscription is not None:
            ledger = self.ledger
            if ledger is not None:
                ledger.unsubscribe(self.subscription)
        self.subscription = None
        self.ledger = None
