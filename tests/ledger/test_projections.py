"""Tests for deterministic ledger projections."""

from __future__ import annotations

import pytest

from xenon.kernel.contracts import (
    FsWrite,
    ReceiptStatus,
    TestRun,
    ToolReceipt,
    VcsCommit,
)
from xenon.ledger.events import EventKind, EventSource, receipt_event_payload
from xenon.ledger.projections import (
    project_all,
    project_audit,
    project_evidence,
    project_plan,
    project_task_state,
)
from xenon.ledger.store import Ledger

TS = "2026-01-01T00:00:00.000+00:00"


def _receipt(tool: str = "mutate") -> ToolReceipt:
    return ToolReceipt(
        receipt_id="r1",
        call_id="c1",
        task_id="t1",
        tool=tool,
        status=ReceiptStatus.OK,
        effects=(
            FsWrite(paths=("calc/add.py",), created_paths=("calc/add.py",)),
            TestRun(target="tests/test_add.py", tests_run=1, passed=1),
            VcsCommit(sha="abc1234", files=("calc/add.py",)),
        ),
        declared_effects=(),
        observed_effects=(),
    )


def _ledger() -> Ledger:
    ledger = Ledger.in_memory("t1")
    ledger.append(
        EventKind.TASK_STARTED,
        EventSource.RUNTIME,
        {"objective": "fix add"},
        ts=TS,
    )
    ledger.append(
        EventKind.TOOL_RECEIPT,
        EventSource.TOOL,
        receipt_event_payload(_receipt()),
        ts=TS,
    )
    return ledger


def test_task_state_collects_artifacts_tests_and_commits() -> None:
    state = project_task_state(_ledger().read())
    assert state["objective"] == "fix add"
    assert state["artifacts"] == ["calc/add.py"]
    assert state["created_artifacts"] == ["calc/add.py"]
    assert state["tests"][0]["passed"] == 1
    assert state["commits"][0]["sha"] == "abc1234"


def test_plan_projection_tracks_plan_and_steps() -> None:
    ledger = Ledger.in_memory("t1")
    ledger.append(
        EventKind.PLAN_ACCEPTED,
        EventSource.RUNTIME,
        {"plan": {"steps": [{"id": 1, "kind": "edit"}]}},
        ts=TS,
    )
    ledger.append(
        EventKind.STEP_STARTED,
        EventSource.RUNTIME,
        {"step_id": "1", "task": "edit add"},
        ts=TS,
    )
    state = project_plan(ledger.read())
    assert state["versions"] == 1
    assert state["steps"]["1"]["status"] == "running"


def test_evidence_projection_tracks_claims_and_receipts() -> None:
    ledger = _ledger()
    ledger.append(
        EventKind.CLAIM_PROPOSED,
        EventSource.LLM,
        {"claim_id": "claim-1", "kind": "file_changed"},
        ts=TS,
    )
    ledger.append(
        EventKind.CLAIM_RESOLVED,
        EventSource.GATE,
        {"claim_id": "claim-1", "status": "bound", "fact_ids": ["fact-1"]},
        ts=TS,
    )
    state = project_evidence(ledger.read())
    assert len(state["receipts"]) == 1
    assert state["claims"][0]["claim_id"] == "claim-1"
    assert state["resolutions"][0]["status"] == "bound"


def test_debt_open_and_close_updates_audit_and_evidence() -> None:
    ledger = Ledger.in_memory("t1")
    ledger.append(
        EventKind.DEBT_OPENED,
        EventSource.GATE,
        {"obligation_id": "o1", "reason": "missing test"},
        ts=TS,
    )
    ledger.append(
        EventKind.DEBT_CLOSED,
        EventSource.GATE,
        {"obligation_id": "o1", "reason": "test passed"},
        ts=TS,
    )
    audit = project_audit(ledger.read())
    evidence = project_evidence(ledger.read())
    assert audit["debt"] == []
    assert evidence["debt"] == []


def test_project_all_and_unknown_projection() -> None:
    ledger = _ledger()
    all_views = project_all(ledger.read())
    assert {"task_state", "plan", "evidence", "audit"} <= set(all_views)
    with pytest.raises(KeyError, match="unknown projection"):
        ledger.project("nope")


def test_noop_write_does_not_become_an_artifact() -> None:
    ledger = Ledger.in_memory("t1")
    ledger.append(
        EventKind.TOOL_RECEIPT,
        EventSource.TOOL,
        receipt_event_payload(
            ToolReceipt(
                receipt_id="r-noop",
                call_id="c-noop",
                task_id="t1",
                tool="mutate",
                status=ReceiptStatus.OK,
                effects=(FsWrite(paths=("calc/add.py",), changed=False),),
            )
        ),
        ts=TS,
    )
    state = project_task_state(ledger.read())
    assert state["artifacts"] == []
