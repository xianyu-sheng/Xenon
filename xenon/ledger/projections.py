"""Deterministic projections over ledger events.

Projections are pure: the same event prefix must produce the same dict (and
therefore the same hash).  They never read the filesystem, the clock or the
network; anything like that belongs in a gateway, not here.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

from xenon.kernel.contracts import (
    FsDelete,
    FsWrite,
    TestRun,
    VcsCommit,
    VcsPush,
)
from xenon.ledger.events import (
    Event,
    EventKind,
    receipt_from_payload,
)

PROJECTION_SCHEMA = "ledger/v1"


def _base(name: str, events: Sequence[Event]) -> dict[str, Any]:
    return {
        "projection": name,
        "schema_version": PROJECTION_SCHEMA,
        "events": len(events),
        "last_seq": events[-1].seq if events else 0,
        "task_id": events[0].task_id if events else "",
    }


def project_task_state(events: Sequence[Event]) -> dict[str, Any]:
    state = _base("task_state", events)
    state.update(
        {
            "objective": "",
            "phase": "created",
            "plan": None,
            "steps": {},
            "artifacts": [],
            "created_artifacts": [],
            "tests": [],
            "commits": [],
            "pushes": [],
            "claims": [],
            "resolutions": [],
            "debt": [],
            "delivered": None,
            "finalized": None,
        }
    )
    artifacts: set[str] = set()
    created: set[str] = set()
    debt: dict[str, Any] = {}

    for event in events:
        payload = event.payload or {}
        if event.kind is EventKind.TASK_STARTED:
            state["objective"] = str(payload.get("objective") or "")
        elif event.kind is EventKind.STATE_TRANSITION:
            state["phase"] = str(payload.get("to") or state["phase"])
        elif event.kind in (EventKind.PLAN_ACCEPTED, EventKind.PLAN_UPDATED):
            state["plan"] = payload.get("plan") or payload
        elif event.kind is EventKind.STEP_STARTED:
            step_id = str(payload.get("step_id") or event.step_id or "")
            state["steps"][step_id] = {
                "task": str(payload.get("task") or ""),
                "status": "running",
            }
        elif event.kind is EventKind.STEP_VERDICT:
            step_id = str(payload.get("step_id") or event.step_id or "")
            item = state["steps"].setdefault(step_id, {})
            item["status"] = str(payload.get("status") or "verdict")
        elif event.kind is EventKind.STEP_CLOSED:
            step_id = str(payload.get("step_id") or event.step_id or "")
            item = state["steps"].setdefault(step_id, {})
            item["status"] = "closed"
        elif event.kind is EventKind.TOOL_RECEIPT:
            receipt_payload = payload.get("receipt")
            if not isinstance(receipt_payload, Mapping):
                continue
            receipt = receipt_from_payload(receipt_payload)
            for effect in receipt.effects:
                if isinstance(effect, FsWrite) and effect.changed:
                    artifacts.update(effect.paths)
                    created.update(effect.created_paths)
                elif isinstance(effect, FsDelete) and effect.changed:
                    artifacts.difference_update(effect.paths)
                    created.difference_update(effect.paths)
                elif isinstance(effect, TestRun):
                    state["tests"].append(
                        {
                            "target": effect.target,
                            "framework": effect.framework,
                            "tests_run": effect.tests_run,
                            "passed": effect.passed,
                            "failed": effect.failed,
                            "errors": effect.errors,
                            "skipped": effect.skipped,
                            "exit_code": effect.exit_code,
                        }
                    )
                elif isinstance(effect, VcsCommit):
                    state["commits"].append(
                        {
                            "sha": effect.sha,
                            "branch": effect.branch,
                            "files": sorted(effect.files),
                        }
                    )
                elif isinstance(effect, VcsPush):
                    state["pushes"].append(
                        {
                            "remote": effect.remote,
                            "branch": effect.branch,
                            "sha": effect.sha,
                        }
                    )
        elif event.kind is EventKind.CLAIM_PROPOSED:
            state["claims"].append(dict(payload))
        elif event.kind is EventKind.CLAIM_RESOLVED:
            state["resolutions"].append(dict(payload))
        elif event.kind is EventKind.DEBT_OPENED:
            obligation_id = str(payload.get("obligation_id") or "")
            if obligation_id:
                debt[obligation_id] = dict(payload)
        elif event.kind is EventKind.DEBT_CLOSED:
            obligation_id = str(payload.get("obligation_id") or "")
            debt.pop(obligation_id, None)
        elif event.kind is EventKind.DELIVERED:
            state["delivered"] = dict(payload)
        elif event.kind is EventKind.FINALIZED:
            state["finalized"] = dict(payload)

    state["artifacts"] = sorted(artifacts)
    state["created_artifacts"] = sorted(created)
    state["debt"] = [debt[key] for key in sorted(debt)]
    return state


def project_plan(events: Sequence[Event]) -> dict[str, Any]:
    state = _base("plan", events)
    state.update({"plan": None, "steps": {}, "versions": 0})
    for event in events:
        if event.kind in (
            EventKind.PLAN_PROPOSED,
            EventKind.PLAN_ACCEPTED,
            EventKind.PLAN_UPDATED,
        ):
            state["plan"] = event.payload.get("plan") or dict(event.payload)
            state["versions"] += 1
        elif event.kind is EventKind.STEP_STARTED:
            step_id = str(event.payload.get("step_id") or event.step_id or "")
            state["steps"][step_id] = {
                "task": str(event.payload.get("task") or ""),
                "status": "running",
            }
        elif event.kind in (EventKind.STEP_VERDICT, EventKind.STEP_CLOSED):
            step_id = str(event.payload.get("step_id") or event.step_id or "")
            item = state["steps"].setdefault(step_id, {})
            item["status"] = (
                "closed"
                if event.kind is EventKind.STEP_CLOSED
                else str(event.payload.get("status") or "verdict")
            )
    return state


def project_evidence(events: Sequence[Event]) -> dict[str, Any]:
    state = _base("evidence", events)
    state.update(
        {
            "receipts": [],
            "claims": [],
            "resolutions": [],
            "verdicts": [],
            "debt": [],
            "audit": [],
            "deliveries": [],
            "finalized": None,
            "legacy_events": [],
        }
    )
    for event in events:
        payload = event.payload or {}
        if event.kind is EventKind.TOOL_RECEIPT:
            receipt_payload = payload.get("receipt")
            if isinstance(receipt_payload, Mapping):
                state["receipts"].append(
                    {
                        "tool": str(receipt_payload.get("tool") or ""),
                        "status": str(receipt_payload.get("status") or ""),
                        "fact_ids": list(payload.get("fact_ids") or ()),
                        "effects": list(receipt_payload.get("effects") or ()),
                    }
                )
        elif event.kind is EventKind.CLAIM_PROPOSED:
            state["claims"].append(dict(payload))
        elif event.kind is EventKind.CLAIM_RESOLVED:
            state["resolutions"].append(dict(payload))
        elif event.kind is EventKind.STEP_VERDICT:
            state["verdicts"].append(dict(payload))
        elif event.kind is EventKind.DEBT_OPENED:
            state["debt"].append(dict(payload))
        elif event.kind is EventKind.DEBT_CLOSED:
            obligation_id = payload.get("obligation_id")
            state["debt"] = [
                item
                for item in state["debt"]
                if item.get("obligation_id") != obligation_id
            ]
        elif event.kind is EventKind.AUDIT_REPORTED:
            state["audit"].append(dict(payload))
        elif event.kind in (EventKind.DELIVERY_ATTEMPTED, EventKind.DELIVERED):
            state["deliveries"].append(dict(payload))
        elif event.kind is EventKind.FINALIZED:
            state["finalized"] = dict(payload)
        elif event.kind is EventKind.LEGACY_SESSION:
            state["legacy_events"].append(
                {
                    "legacy_id": payload.get("legacy_id"),
                    "legacy_type": payload.get("legacy_type"),
                }
            )
    return state


def project_audit(events: Sequence[Event]) -> dict[str, Any]:
    state = _base("audit", events)
    state.update({"obligations": [], "debt": [], "coverage": [], "gaps": []})
    for event in events:
        payload = event.payload or {}
        if event.kind is EventKind.AUDIT_REPORTED:
            state["coverage"].extend(dict(item) for item in payload.get("coverage") or ())
            state["gaps"].extend(dict(item) for item in payload.get("gaps") or ())
            state["obligations"].extend(
                dict(item) for item in payload.get("obligations") or ()
            )
        elif event.kind is EventKind.DEBT_OPENED:
            state["debt"].append(dict(payload))
        elif event.kind is EventKind.DEBT_CLOSED:
            obligation_id = payload.get("obligation_id")
            state["debt"] = [
                item
                for item in state["debt"]
                if item.get("obligation_id") != obligation_id
            ]
    return state


PROJECTIONS: Mapping[str, Callable[[Sequence[Event]], dict[str, Any]]] = {
    "task": project_task_state,
    "task_state": project_task_state,
    "plan": project_plan,
    "evidence": project_evidence,
    "audit": project_audit,
}


def project(name: str, events: Iterable[Event]) -> dict[str, Any]:
    try:
        function = PROJECTIONS[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown projection {name!r}; available: {sorted(PROJECTIONS)}"
        ) from exc
    return function(tuple(events))


def project_all(events: Iterable[Event]) -> dict[str, dict[str, Any]]:
    materialized = tuple(events)
    return {
        name: function(materialized)
        for name, function in PROJECTIONS.items()
        if name != "task"
    }
