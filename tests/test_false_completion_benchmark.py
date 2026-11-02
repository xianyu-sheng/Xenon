"""The false-completion benchmark must stay honest (#288).

Two things this test defends:

1. all nine cases run and are reported (the denominator never shrinks);
2. the harness refuses the adversarial ones — if a case starts passing, the
   benchmark tells us instead of quietly turning green.
"""

from __future__ import annotations

import json

from evals.false_completion import benchmark_to_json, run_benchmark


def test_all_nine_cases_are_reported():
    report = run_benchmark()

    assert report["total"] == 9
    assert len(report["cases"]) == 9
    assert {case["id"] for case in report["cases"]} == {
        "success",
        "partial_requirement",
        "false_test_claim",
        "stray_changes",
        "repair_then_succeed",
        "semantic_abstain",
        "no_push",
        "noop",
        "tool_failure_recovery",
    }


def test_every_case_matches_its_expectation():
    report = run_benchmark()

    assert report["mismatches"] == [], json.dumps(
        [c for c in report["cases"] if c["verdict"] != "as expected"],
        ensure_ascii=False,
        indent=2,
    )


def test_no_false_completion_is_recorded():
    """Delivered-as-verified while the harness never verified = bug."""

    report = run_benchmark()

    assert report["false_completion_count"] == 0


def test_the_healthy_case_still_delivers():
    """A benchmark that only proves "everything blocks" is useless."""

    report = run_benchmark()
    success = next(c for c in report["cases"] if c["id"] == "success")

    assert success["actual_accepted"] is True
    assert success["actual_evidence"] == "verified"


def test_the_accepted_gap_delivers_but_stays_partial():
    report = run_benchmark()
    accepted = next(c for c in report["cases"] if c["id"] == "semantic_abstain")

    assert accepted["actual_accepted"] is True
    assert accepted["actual_evidence"] == "partial", (
        "delivering an accepted gap must never read as verified"
    )


def test_the_report_labels_the_model_as_a_fake():
    report = run_benchmark()

    assert "scripted-fake" in report["model"]
    assert "NOT a real model run" in report["model"]


def test_the_report_keeps_raw_per_case_data():
    report = run_benchmark()

    for case in report["cases"]:
        assert case["events"] > 0
        assert "expect_phase" in case and "actual_phase" in case
    assert "calls_to_completion" in report
    assert len(report["calls_to_completion"]) == 9


def test_the_json_report_round_trips():
    report = run_benchmark()

    parsed = json.loads(benchmark_to_json(report))

    assert parsed["total"] == 9
