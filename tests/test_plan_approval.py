"""Kernel planned-plan approval is a session decision (#278)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import xenon.kernel  # noqa: F401 — break the ledger/kernel import cycle
from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.store import Ledger
from xenon.repl.command_groups.plan import _cmd_plan
from xenon.repl.kernel_evidence import (
    load_kernel_evidence,
    plan_approval,
    planned_plan,
)


def _plan_dict() -> dict:
    return {
        "analysis": "inspect then edit",
        "steps": [
            {
                "step_id": 1,
                "kind": "inspect",
                "targets": ["calc/add.py"],
                "depends_on": [],
                "prefer": None,
                "intent": {
                    "capability": "read",
                    "params": {"file_path": "calc/add.py"},
                    "targets": ["calc/add.py"],
                    "prefer": None,
                },
                "expects": [],
            },
            {
                "step_id": 2,
                "kind": "edit",
                "targets": ["calc/add.py"],
                "depends_on": [1],
                "prefer": None,
                "intent": {
                    "capability": "edit",
                    "params": {"files": [{"path": "calc/add.py"}]},
                    "targets": ["calc/add.py"],
                    "prefer": None,
                },
                "expects": [
                    {
                        "id": "edit-1",
                        "kind": "file_changed",
                        "target": "calc/add.py",
                        "expect": {},
                        "since_seq": 0,
                    }
                ],
            },
        ],
    }


def _make_planned_ledger(directory: Path, task_id: str = "planned") -> Path:
    path = directory / f"{task_id}.jsonl"
    ledger = Ledger(path, task_id)
    plan = _plan_dict()
    ledger.append(
        EventKind.TASK_STARTED,
        EventSource.RUNTIME,
        {
            "objective": "inspect then edit",
            "user_objective": "inspect then edit",
            "workspace": str(directory),
        },
    )
    ledger.append(EventKind.PLAN_PROPOSED, EventSource.LLM, {"plan": plan})
    ledger.append(EventKind.PLAN_ACCEPTED, EventSource.RUNTIME, {"plan": plan})
    ledger.append(
        EventKind.STATE_TRANSITION,
        EventSource.RUNTIME,
        {"from": "running", "to": "planned"},
    )
    path.with_suffix(".result.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "accepted": False,
                "phase": "planned",
                "answer": "1. inspect\n2. edit",
                "evidence_status": "unverified",
            }
        ),
        encoding="utf-8",
    )
    return path


class ApprovalRepl:
    def __init__(self, path: Path) -> None:
        self._last_kernel_ledger = path
        self._last_kernel_task_id = path.stem
        self._plan_mode_active = True
        self.events: list[dict] = []
        self._session_events = SimpleNamespace(read=lambda: list(self.events))

    def _record_event(self, event_type: str, **data) -> str:
        event = {
            "id": f"event-{len(self.events)}",
            "type": event_type,
            **data,
        }
        self.events.append(event)
        return event["id"]


@pytest.fixture()
def planned(tmp_path: Path, monkeypatch) -> tuple[Path, ApprovalRepl]:
    import xenon.repl.kernel_evidence as module

    monkeypatch.setattr(module, "kernel_ledger_dir", lambda: tmp_path)
    path = _make_planned_ledger(tmp_path)
    return path, ApprovalRepl(path)


def test_planned_plan_parses_intents_and_expectations(planned) -> None:
    path, _repl = planned
    evidence = load_kernel_evidence(path)
    assert evidence is not None
    extracted = planned_plan(evidence)
    assert extracted is not None
    assert extracted.task_id == "planned"
    assert [step.id for step in extracted.plan.steps] == [1, 2]
    assert extracted.plan.steps[0].intent.capability.value == "read"
    assert extracted.plan.steps[1].depends_on == (1,)
    assert extracted.plan.steps[1].expects[0].id == "edit-1"
    assert len(extracted.digest) == 64
    assert plan_approval(_repl.events, "planned") is None


def test_plan_approve_records_binding_and_exits_plan_mode(planned) -> None:
    path, repl = planned
    before = len(Ledger(path, "planned").read())

    out = _cmd_plan(args="approve", session_state={"_repl": repl})

    assert "已批准并绑定" in out
    assert repl._plan_mode_active is False
    assert [event["type"] for event in repl.events] == [
        "plan/approved",
        "plan_mode/set",
    ]
    binding = repl.events[0]
    assert binding["task_id"] == "planned"
    assert binding["ledger"] == str(path)
    assert binding["step_count"] == 2
    assert len(binding["plan_digest"]) == 64
    assert len(Ledger(path, "planned").read()) == before
    assert plan_approval(repl.events, "planned")["status"] == "approved"

    again = _cmd_plan(args="approve", session_state={"_repl": repl})
    assert "无需重复审批" in again
    assert len(repl.events) == 2


def test_plan_reject_keeps_plan_mode(planned) -> None:
    _path, repl = planned

    out = _cmd_plan(args="reject", session_state={"_repl": repl})

    assert "已驳回" in out
    assert repl._plan_mode_active is True
    assert [event["type"] for event in repl.events] == ["plan/rejected"]
    assert plan_approval(repl.events, "planned")["status"] == "rejected"


def test_plan_status_shows_pending_and_approved(planned) -> None:
    _path, repl = planned

    assert "计划待确认" in _cmd_plan(
        args="status", session_state={"_repl": repl}
    )

    _cmd_plan(args="approve", session_state={"_repl": repl})
    status = _cmd_plan(args="status", session_state={"_repl": repl})
    assert "计划已批准（尚未执行）" in status


def test_plan_approve_fails_closed_when_ledger_is_not_planned(
    planned,
) -> None:
    path, repl = planned
    result = json.loads(path.with_suffix(".result.json").read_text("utf-8"))
    result["phase"] = "finalized"
    path.with_suffix(".result.json").write_text(
        json.dumps(result), encoding="utf-8"
    )

    out = _cmd_plan(args="approve", session_state={"_repl": repl})

    assert "不是待确认计划" in out
    assert repl.events == []


def test_plan_approve_fails_closed_when_chain_is_tampered(planned) -> None:
    path, repl = planned
    lines = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[0])
    payload["payload"]["objective"] = "tampered"
    lines[0] = json.dumps(payload, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    out = _cmd_plan(args="approve", session_state={"_repl": repl})

    assert "链无效" in out
    assert repl.events == []
