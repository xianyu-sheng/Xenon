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
    # A violated verdict is unfinished work, not a contradiction.
    assert {item["kind"] for item in report["contradictions"]} == {
        "claim_not_bound"
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


def test_comment_form_leads_with_a_verdict() -> None:
    from xenon.report import render_comment

    report = {
        "task": {"id": "t1", "model": "m", "objective": "o"},
        "status": {
            "evidence_status": "partial",
            "answer_verified": False,
            "delivered": False,
            "commit": "",
        },
        "integrity": {"valid": True, "events": 3, "last_hash": "abc"},
        "tools": {"calls": 2, "failures": 1, "tests": [
            {"passed": 1, "failed": 0, "command": "pytest", "exit_code": 0}
        ]},
        "delivery": {},
        "claims": [
            {"kind": "file_changed", "target": "a.py", "status": "unresolved"},
        ],
        "contradictions": [],
        "gaps": [{"id": "o1", "reason": "no changed file fact"}],
        "replay": {"ledger": "x.jsonl", "command": "xenon replay t1"},
        "limits": {"note": "n"},
    }
    text = render_comment(report)
    assert text.startswith("### ⚠️")
    assert "绑定失败 1 条" in text
    assert "1 passed" in text
    assert "no changed file fact" in text
    assert "<details>" in text


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


def test_gate_passes_only_when_every_obligation_is_met() -> None:
    from xenon.report import gate_verdict

    verdict = gate_verdict(
        evidence_status="verified",
        answer_verified=True,
        contradictions=(),
        gaps=(),
        integrity_valid=True,
        delivered=True,
    )
    assert verdict["decision"] == "pass"
    assert verdict["reasons"] == []


def test_gate_blocks_a_claim_the_facts_refuse() -> None:
    """A contradiction is not "unfinished" — it is a claim that is wrong."""
    from xenon.report import gate_verdict

    verdict = gate_verdict(
        evidence_status="contradicted",
        answer_verified=False,
        contradictions=({"kind": "claim_not_bound"},),
        gaps=(),
        integrity_valid=True,
        delivered=True,
    )
    assert verdict["decision"] == "block"
    assert any("do not support" in item for item in verdict["reasons"])


def test_gate_blocks_an_unsound_ledger_even_when_nothing_else_is_wrong() -> None:
    """Everything downstream reads the ledger; if it does not verify, nothing
    drawn from it can be acted on."""
    from xenon.report import gate_verdict

    verdict = gate_verdict(
        evidence_status="verified",
        answer_verified=True,
        contradictions=(),
        gaps=(),
        integrity_valid=False,
        delivered=True,
    )
    assert verdict["decision"] == "block"
    assert verdict["summary"] == "the ledger itself does not verify"


def test_gate_warns_rather_than_passing_an_unfinished_run() -> None:
    """`finalized` is not success.

    A measured run finished with ``phase=finalized`` while having changed no
    file and met no obligation.  Reading the phase as "done" is exactly the
    false assurance the product must not produce, so the gate refuses to pass
    it.
    """
    from xenon.report import gate_verdict

    verdict = gate_verdict(
        evidence_status="partial",
        answer_verified=False,
        contradictions=(),
        gaps=({"id": "v2-file_changed-1"}, {"id": "v2-test_passed-2"}),
        integrity_valid=True,
        delivered=True,
    )
    assert verdict["decision"] == "warn"
    assert any("2 obligation" in item for item in verdict["reasons"])


def test_gate_does_not_pass_a_run_that_delivered_nothing() -> None:
    from xenon.report import gate_verdict

    verdict = gate_verdict(
        evidence_status="unverified",
        answer_verified=False,
        contradictions=(),
        gaps=(),
        integrity_valid=True,
        delivered=False,
    )
    assert verdict["decision"] == "warn"
    assert any("no answer" in item for item in verdict["reasons"])


def test_the_report_carries_its_schema_version_and_gate(tmp_path: Path) -> None:
    """A CI gate must be able to refuse a report it does not understand."""
    from xenon.report import SCHEMA_VERSION

    report = build_report(_ledger(tmp_path))

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["gate"]["decision"] in {"pass", "warn", "block"}
    assert isinstance(report["gate"]["reasons"], list)
