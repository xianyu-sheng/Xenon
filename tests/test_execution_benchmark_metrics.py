"""Offline aggregation contract for the live execution benchmark."""

from __future__ import annotations

from evals.execution_benchmark import CaseResult, aggregate_results, classify_failure


def _result(**updates) -> CaseResult:
    data = dict(
        name="calc",
        run=1,
        ok=True,
        reason="",
        elapsed=10.0,
        tools=5,
        failures=0,
        task_status="completed",
        missing=[],
        commit="abc1234",
        test_returncode=0,
        result_tail="",
        warnings=[],
        task_state={},
    )
    data.update(updates)
    return CaseResult(**data)


def test_empty_aggregate_is_zero_and_does_not_divide_by_zero() -> None:
    metrics = aggregate_results([])

    assert metrics["runs"] == 0
    assert metrics["completion_rate"] == 0.0
    assert metrics["repair_success_rate"] is None
    assert metrics["median_duration_sec"] == 0.0
    assert metrics["failure_categories"] == {}


def test_mixed_aggregate_keeps_failed_runs_in_denominator() -> None:
    results = [
        _result(run=1, elapsed=10.0, tools=4, failures=1),
        _result(
            run=2,
            ok=False,
            reason="worktree dirty: mathx.py",
            elapsed=20.0,
            tools=8,
            failures=0,
            task_status="active",
        ),
        _result(
            run=3,
            ok=False,
            reason="target tests failed: 1 failed",
            elapsed=30.0,
            tools=6,
            failures=2,
            task_status="completed",
        ),
    ]

    metrics = aggregate_results(results)

    assert metrics["runs"] == 3
    assert metrics["passed"] == 1
    assert metrics["failed"] == 2
    assert metrics["completion_rate"] == 0.3333
    assert metrics["false_completion_count"] == 1
    assert metrics["runs_with_tool_failures"] == 2
    assert metrics["repair_successes"] == 1
    assert metrics["repair_success_rate"] == 0.5
    assert metrics["median_duration_sec"] == 20.0
    assert metrics["median_tool_calls"] == 6.0
    assert metrics["failure_categories"] == {
        "dirty_worktree": 1,
        "test_failure": 1,
    }


def test_p95_uses_nearest_rank_without_hiding_outliers() -> None:
    results = [_result(run=i, elapsed=float(i), tools=i) for i in range(1, 21)]

    metrics = aggregate_results(results)

    assert metrics["p95_duration_sec"] == 19.0
    assert metrics["p95_tool_calls"] == 19.0


def test_failure_taxonomy_is_stable() -> None:
    assert classify_failure("TaskState missing: changes") == "no_source_write"
    assert classify_failure("worktree dirty: calc.py") == "dirty_worktree"
    assert classify_failure("no new commit") == "missing_commit"
    assert classify_failure("target tests failed: 1 failed") == "test_failure"
    assert classify_failure("SecurityError: path escape") == "tool_security"
    assert classify_failure("command timed out") == "timeout"
    assert (
        classify_failure("402 Payment Required for https://api.deepseek.com")
        == "api_or_credentials"
    )
    assert classify_failure("TaskState status: active") == "task_state_incomplete"
    assert classify_failure("") == "none"


def test_infrastructure_failures_are_visible_but_not_hidden_from_denominator() -> None:
    results = [
        _result(run=1),
        _result(
            run=2,
            ok=False,
            reason="402 Payment Required: https://api.deepseek.com",
            task_status="active",
        ),
    ]

    metrics = aggregate_results(results)

    assert metrics["completion_rate"] == 0.5
    assert metrics["infrastructure_failure_count"] == 1
    assert metrics["completion_rate_excluding_infrastructure"] == 1.0
    assert metrics["failure_categories"] == {"api_or_credentials": 1}
