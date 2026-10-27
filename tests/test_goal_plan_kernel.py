"""Kernel-backed /goal and /plan status projections (#238)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from xenon.kernel.contracts import FsWrite, ReceiptStatus, ToolReceipt
from xenon.ledger.events import (
    EventKind,
    EventSource,
    receipt_event_payload,
)
from xenon.ledger.store import Ledger
from xenon.repl.command_groups.goal import _cmd_goal
from xenon.repl.command_groups.plan import _cmd_plan
from xenon.repl.kernel_evidence import (
    goal_lines,
    load_kernel_evidence,
    plan_lines,
)


def _make_kernel_ledger(directory: Path, task_id: str = "goal-kernel") -> Path:
    path = directory / f"{task_id}.jsonl"
    ledger = Ledger(path, task_id)
    ledger.append(
        EventKind.TASK_STARTED,
        EventSource.RUNTIME,
        {"objective": "fix add", "workspace": str(directory)},
    )
    plan = {
        "analysis": "inspect then edit",
        "steps": [
            {
                "step_id": 1,
                "kind": "inspect",
                "targets": ["calc/add.py"],
                "depends_on": [],
                "intent": None,
                "expects": [],
            }
        ],
    }
    ledger.append(EventKind.PLAN_PROPOSED, EventSource.LLM, {"plan": plan})
    ledger.append(EventKind.PLAN_ACCEPTED, EventSource.RUNTIME, {"plan": plan})
    ledger.append(
        EventKind.STEP_STARTED,
        EventSource.RUNTIME,
        {"step_id": 1, "task": task_id, "kind": "inspect"},
        step_id="1",
    )
    ledger.append(
        EventKind.STEP_CLOSED,
        EventSource.RUNTIME,
        {"step_id": 1, "status": "satisfied"},
        step_id="1",
    )
    ledger.append(
        EventKind.DEBT_OPENED,
        EventSource.GATE,
        {
            "obligation_id": "o1",
            "kind": "test_passed",
            "target": "tests/test_add.py",
            "reason": "test not run",
        },
    )
    receipt = ToolReceipt(
        receipt_id="r-write",
        call_id="c-write",
        task_id=task_id,
        tool="apply_patch",
        status=ReceiptStatus.OK,
        effects=(FsWrite(paths=("calc/add.py",), hashes={"calc/add.py": "h"}),),
    )
    ledger.append(
        EventKind.TOOL_RECEIPT,
        EventSource.TOOL,
        receipt_event_payload(receipt),
    )
    ledger.append(
        EventKind.STATE_TRANSITION,
        EventSource.RUNTIME,
        {"from": "running", "to": "finalized"},
    )
    ledger.append(
        EventKind.FINALIZED,
        EventSource.RUNTIME,
        {"commit": "abc1234", "evidence_status": "verified"},
    )
    path.with_suffix(".result.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "accepted": True,
                "evidence_status": "verified",
            }
        ),
        encoding="utf-8",
    )
    return path


def _repl(path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        _last_kernel_ledger=path,
        _plan_mode_active=False,
        _session_events=SimpleNamespace(
            read=lambda: [], path=path.parent / "legacy.jsonl"
        ),
    )


def test_kernel_goal_lines_include_plan_debt_artifact_and_finalize(
    tmp_path: Path, monkeypatch
) -> None:
    import xenon.repl.kernel_evidence as module

    monkeypatch.setattr(module, "kernel_ledger_dir", lambda: tmp_path)
    path = _make_kernel_ledger(tmp_path)
    evidence = load_kernel_evidence(path)
    assert evidence is not None

    lines = goal_lines(evidence)
    text = "\n".join(lines)
    assert "Kernel 目标：fix add" in text
    assert "[closed] inspect" in text
    assert "未结债务 1" in text
    assert "calc/add.py" in text
    assert "abc1234" in text
    assert "链: " not in text


def test_goal_command_prefers_kernel_ledger(tmp_path: Path, monkeypatch) -> None:
    import xenon.repl.kernel_evidence as module

    monkeypatch.setattr(module, "kernel_ledger_dir", lambda: tmp_path)
    path = _make_kernel_ledger(tmp_path, "goal-command")
    out = _cmd_goal(session_state={"_repl": _repl(path)})
    assert "Kernel 目标" in out
    assert "goal-command" not in out
    assert "fix add" in out
    assert "未结债务" in out


def test_plan_status_adds_kernel_plan(tmp_path: Path, monkeypatch) -> None:
    import xenon.repl.kernel_evidence as module

    monkeypatch.setattr(module, "kernel_ledger_dir", lambda: tmp_path)
    path = _make_kernel_ledger(tmp_path, "plan-command")
    repl = _repl(path)
    out = _cmd_plan(args="status", session_state={"_repl": repl})
    assert "计划模式：关闭" in out
    assert "Kernel 计划（1 步）" in out
    assert "[closed] inspect" in out


def test_plan_lines_do_not_invent_steps(tmp_path: Path, monkeypatch) -> None:
    import xenon.repl.kernel_evidence as module

    monkeypatch.setattr(module, "kernel_ledger_dir", lambda: tmp_path)
    path = tmp_path / "no-plan.jsonl"
    ledger = Ledger(path, "no-plan")
    ledger.append(
        EventKind.TASK_STARTED,
        EventSource.RUNTIME,
        {"objective": "no plan", "workspace": str(tmp_path)},
    )
    evidence = load_kernel_evidence(path)
    assert evidence is not None
    assert plan_lines(evidence) == ["🧭 Kernel 计划：（无）"]


def test_missing_kernel_ledger_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    import xenon.repl.kernel_evidence as module

    monkeypatch.setattr(module, "kernel_ledger_dir", lambda: tmp_path)
    repl = _repl(tmp_path / "missing.jsonl")
    goal = _cmd_goal(session_state={"_repl": repl})
    plan = _cmd_plan(args="status", session_state={"_repl": repl})
    assert "不可读" in goal
    assert "不退回 legacy" in goal
    assert "不可读" in plan


def test_goal_command_marks_tampered_chain(tmp_path: Path, monkeypatch) -> None:
    import xenon.repl.kernel_evidence as module

    monkeypatch.setattr(module, "kernel_ledger_dir", lambda: tmp_path)
    path = _make_kernel_ledger(tmp_path, "goal-tamper")
    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    records[0]["payload"]["objective"] = "tampered"
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n",
        encoding="utf-8",
    )
    repl = _repl(path)
    out = _cmd_goal(session_state={"_repl": repl})
    assert "链无效" in out
    assert "仅供取证" in out
    plan = _cmd_plan(args="status", session_state={"_repl": repl})
    assert "链无效" in plan
