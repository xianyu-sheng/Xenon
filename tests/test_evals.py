from __future__ import annotations

from pathlib import Path

from evals.runner import load_tasks, run_eval, summarize, write_report


def test_loads_twenty_eval_tasks():
    tasks = load_tasks()
    assert len(tasks) == 20
    for task in tasks:
        assert {"id", "category", "prompt", "expected_tools", "success_criteria"} <= set(task)
        assert isinstance(task["expected_tools"], list)


def test_mock_eval_reports_core_metrics(tmp_path: Path):
    tasks = load_tasks()
    results = run_eval(tasks, mode="mock")
    summary = summarize(results)

    assert summary["tasks"] == 20
    assert summary["success_rate"] == 100.0
    assert summary["average_tokens"] > 0
    assert summary["tool_calls"] >= 20
    assert summary["tool_failures"] == 0

    report_path = write_report(
        results,
        tmp_path / "mock_report.md",
        mode="mock",
        model="mock-agent",
        run_date="2026-06-08 00:00:00 UTC",
    )
    report = report_path.read_text(encoding="utf-8")
    assert "Success Rate: 100.0%" in report
    assert "Average Tokens:" in report
    assert "Tool Failures: 0" in report
    assert "`edit-python-function`" in report
