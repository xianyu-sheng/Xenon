"""Delivery report v0: the artifact a reviewer reads (#423)."""

from __future__ import annotations

import json
from pathlib import Path

from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.store import Ledger
from xenon.replay import resolve_ledger
from xenon.report import build_report, render_markdown


def _ledger(tmp_path: Path) -> Path:
    path = tmp_path / "t1.jsonl"
    ledger = Ledger(path, "t1")
    ledger.append(
        EventKind.TASK_STARTED,
        EventSource.RUNTIME,
        {
            "task_id": "t1",
            "objective": f"fix {Path.home()}/secret and ghp_{'a' * 24}",
            "user_objective": "fix the calculator",
            "workspace": str(tmp_path),
            "constraints": ["runtime:kernel", "model:test/fake"],
            "mode": "auto",
            "obligations": [{"id": "o1", "kind": "file_changed"}],
        },
    )
    ledger.append(
        EventKind.CLAIM_PROPOSED,
        EventSource.LLM,
        {
            "claim_id": "claim-1",
            "kind": "file_changed",
            "target": "calc/add.py",
            "source": "llm",
        },
    )
    ledger.append(
        EventKind.CLAIM_RESOLVED,
        EventSource.GATE,
        {
            "claim_id": "claim-1",
            "status": "unresolved",
            "reason": "no matching receipt",
            "fact_ids": [],
        },
    )
    ledger.append(
        EventKind.STEP_VERDICT,
        EventSource.GATE,
        {
            "obligation_id": "o1",
            "stage": "v1",
            "status": "violated",
            "reason": "expected a write, saw none",
        },
    )
    ledger.append(
        EventKind.DEBT_OPENED,
        EventSource.GATE,
        {
            "obligation_id": "o1",
            "stage": "v1",
            "kind": "file_changed",
            "target": "calc/add.py",
            "reason": "expected a write, saw none",
            "attempts": 1,
        },
    )
    ledger.append(
        EventKind.TOOL_RECEIPT,
        EventSource.TOOL,
        {
            "fact_ids": [],
            "receipt": {
                "tool": "bash",
                "status": "ok",
                "effects": [
                    {
                        "type": "test_run",
                        "command": "python -m pytest -q",
                        "target": "tests",
                        "passed": 2,
                        "failed": 0,
                        "errors": 0,
                        "skipped": 0,
                        "exit_code": 0,
                    }
                ],
            },
        },
    )
    ledger.append(
        EventKind.FINALIZE_STARTED,
        EventSource.RUNTIME,
        {"verified_paths": [], "unverified_paths": ["calc/add.py"], "commit": ""},
    )
    ledger.append(
        EventKind.DELIVERED,
        EventSource.RUNTIME,
        {"answer": "done, allegedly", "claims": []},
    )
    ledger.append(
        EventKind.FINALIZED,
        EventSource.RUNTIME,
        {"commit": "", "evidence_status": "partial"},
    )
    return path


def test_report_separates_receipt_evidence_from_answer_evidence(
    tmp_path: Path,
) -> None:
    report = build_report(_ledger(tmp_path))

    assert report["status"]["evidence_status"] == "partial"
    assert report["status"]["answer_verified"] is False
    assert report["claims"][0]["status"] == "unresolved"
    assert {item["kind"] for item in report["contradictions"]} == {
        "claim_not_bound",
        "verdict_violated",
    }
    assert report["gaps"][0]["id"] == "o1"
    assert report["tools"]["tests"][0]["passed"] == 2
    assert report["integrity"]["valid"] is True


def test_report_redacts_home_and_tokens(tmp_path: Path) -> None:
    report = build_report(_ledger(tmp_path))
    objective = report["task"]["objective"]
    assert str(Path.home()) not in objective
    assert "ghp_" not in objective
    assert "[redacted]" in objective


def test_markdown_states_answer_is_unverified(tmp_path: Path) -> None:
    text = render_markdown(build_report(_ledger(tmp_path)))
    assert "**Claims:** 1" in text
    assert "**Answer:** `unverified`" in text
    assert "Where claims and facts disagree" in text


def test_resolve_ledger_finds_repl_subdirectory(tmp_path: Path) -> None:
    target = tmp_path / "repl" / "repl-abc.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    assert resolve_ledger("repl-abc", root=tmp_path) == target


def test_report_is_deterministic_for_the_same_ledger(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    first = json.dumps(build_report(path), sort_keys=True, ensure_ascii=False)
    second = json.dumps(build_report(path), sort_keys=True, ensure_ascii=False)
    assert first == second


def test_cli_report_writes_the_artifact(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from xenon.main import _cmd_report

    path = _ledger(tmp_path)
    out = tmp_path / "reports"
    code = _cmd_report(
        str(path), SimpleNamespace(json=False, out=str(out))
    )
    assert code == 0
    assert (out / f"{path.stem}.report.md").is_file()
    assert "Delivery Report" in (out / f"{path.stem}.report.md").read_text(
        encoding="utf-8"
    )
