"""Deterministic test-execution evidence helpers."""

from __future__ import annotations

from xenon.engine.evidence_contracts import (
    command_is_test_like,
    is_non_test_invocation,
    parse_test_counts,
)
from xenon.engine.evidence_contracts import (
    tests_run_from_metadata as _tests_run_from_metadata,
)


def test_parse_pytest_and_unittest_counts() -> None:
    assert parse_test_counts("2 passed, 1 failed in 0.12s")["tests_run"] == 3
    assert parse_test_counts("Ran 4 tests in 0.01s\n\nOK")["tests_run"] == 4
    assert parse_test_counts("collected 2 items")["tests_run"] == 0


def test_metadata_test_count_wins_over_absent_output() -> None:
    assert _tests_run_from_metadata({"tests_run": 3}, "") == 3
    assert _tests_run_from_metadata(None, "1 passed") == 1


def test_probe_commands_are_not_test_execution() -> None:
    assert command_is_test_like("python -m pytest -q")
    assert is_non_test_invocation("pytest --version")
    assert is_non_test_invocation("python -m pytest --collect-only")
    assert not is_non_test_invocation("pytest tests/test_app.py")
