"""Request budget: one deadline and one pacing sequence per run.

The policy moved out of the engine package (#202) but its guarantees did not
change: a run with many model calls must still respect a single wall clock,
and a provider with a low RPM limit must still see paced requests.
"""

from __future__ import annotations

import time

import pytest

from xenon.harness.request_budget import (
    DeadlineExceeded,
    RequestBudget,
    timeout_from_env,
)


def test_from_timeout_sets_an_absolute_deadline():
    budget = RequestBudget.from_timeout(30)

    assert budget.timeout_seconds == 30
    remaining = budget.remaining()
    assert remaining is not None and 0 < remaining <= 30


def test_from_timeout_rejects_a_non_positive_timeout():
    with pytest.raises(ValueError, match="positive"):
        RequestBudget.from_timeout(0)


def test_request_budget_is_capped_by_the_remaining_deadline():
    budget = RequestBudget.from_timeout(5, request_timeout=120)

    assert budget.request_budget() <= 5


def test_expired_budget_never_hands_a_zero_timeout_to_the_client():
    expired = RequestBudget(deadline_at=time.monotonic() - 1)

    with pytest.raises(DeadlineExceeded):
        expired.request_budget()


def test_expired_budget_refuses_to_start_work():
    budget = RequestBudget(deadline_at=time.monotonic() - 1)
    seen: list[str] = []
    budget.event_sink = lambda event, fields: seen.append(event)

    with pytest.raises(DeadlineExceeded):
        budget.check("plan")

    assert seen == ["deadline_exceeded"]


def test_no_deadline_means_unbounded():
    budget = RequestBudget(deadline_at=None, request_timeout=42)

    assert budget.remaining() is None
    assert budget.request_budget() == 42
    budget.check()  # must not raise


def test_pacing_spaces_request_slots():
    budget = RequestBudget.from_timeout(10, min_request_interval=0.05)
    seen: list[tuple[str, dict]] = []
    budget.event_sink = lambda event, fields: seen.append((event, fields))

    started = time.monotonic()
    for _ in range(3):
        budget.wait_for_request_slot("provider")
    elapsed = time.monotonic() - started

    assert elapsed >= 0.09, "three slots at 50ms must not collapse into one"
    assert [event for event, _ in seen].count("provider_pacing") == 2


def test_pacing_without_an_interval_does_not_sleep():
    budget = RequestBudget.from_timeout(10, min_request_interval=0.0)
    started = time.monotonic()

    for _ in range(20):
        budget.wait_for_request_slot()

    assert time.monotonic() - started < 0.05


def test_sleep_refuses_to_overrun_the_deadline():
    budget = RequestBudget(deadline_at=time.monotonic() + 0.01, timeout_seconds=0.01)

    with pytest.raises(DeadlineExceeded):
        budget.sleep(5.0)


def test_sleep_is_a_no_op_within_the_budget():
    budget = RequestBudget(deadline_at=time.monotonic() + 5)

    budget.sleep(0.01)  # must not raise


@pytest.mark.parametrize(
    "kwargs",
    [
        {"request_timeout": 0},
        {"provider_attempts": 0},
        {"chain_retries": -1},
        {"min_request_interval": -0.1},
        {"provider_attempts": 2, "chain_retries": 1},
    ],
)
def test_incoherent_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        RequestBudget(deadline_at=None, **kwargs)


def test_timeout_from_env_honours_the_override(monkeypatch):
    monkeypatch.setenv("XENON_ENGINE_TIMEOUT", "42")

    assert timeout_from_env() == 42


@pytest.mark.parametrize("raw", ["", "nonsense", "0", "-5"])
def test_timeout_from_env_falls_back_instead_of_disabling(monkeypatch, raw):
    monkeypatch.setenv("XENON_ENGINE_TIMEOUT", raw)

    assert timeout_from_env(99) == 99


def test_pacing_refuses_when_the_next_slot_would_overrun_the_deadline():
    """Pacing must fail closed instead of sleeping past the deadline."""

    budget = RequestBudget(
        deadline_at=time.monotonic() + 0.05,
        timeout_seconds=0.05,
        min_request_interval=5.0,
    )
    budget.wait_for_request_slot()  # reserves a slot far in the future

    with pytest.raises(DeadlineExceeded, match="pacing"):
        budget.wait_for_request_slot()


def test_sleep_reports_the_exhausted_phase():
    budget = RequestBudget(deadline_at=time.monotonic() - 1)
    seen: list[tuple[str, dict]] = []
    budget.event_sink = lambda event, fields: seen.append((event, fields))

    with pytest.raises(DeadlineExceeded):
        budget.sleep(0.1, phase="retry_backoff")

    assert seen and seen[0][1]["phase"] == "retry_backoff"
