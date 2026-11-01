"""Cancellation (#327): Ctrl-C during a model call must actually stop it."""

from __future__ import annotations

import time

import pytest

from tests.kernel.test_north_star import _runtime
from xenon.harness.llm import ModelConfig, _call_model
from xenon.kernel.cancel import ModelCancelled
from xenon.kernel.contracts import TaskPhase, TaskSpec
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


def test_call_without_predicate_stays_synchronous():
    """No predicate means no thread: tests, SDK and evals keep the plain path."""

    seen: list[str] = []

    def client(model_id, messages, **kwargs):
        seen.append(model_id)
        return "ok"

    assert _call_model(ModelConfig(model_id="m"), client, []) == "ok"
    assert seen == ["m"]


def test_cancel_interrupts_an_in_flight_call():
    cancelled = {"value": False}

    def slow_client(model_id, messages, **kwargs):
        time.sleep(5)
        return "too late"

    def should_cancel() -> bool:
        return cancelled["value"]

    import threading

    timer = threading.Timer(0.1, lambda: cancelled.update(value=True))
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(ModelCancelled):
            _call_model(
                ModelConfig(
                    model_id="m", should_cancel=should_cancel
                ),
                slow_client,
                [],
            )
    finally:
        timer.cancel()

    assert time.monotonic() - started < 2.0, "a cancel must not wait for the call"


def test_call_errors_are_re_raised_unchanged():
    class Boom(RuntimeError):
        pass

    def client(model_id, messages, **kwargs):
        raise Boom("provider said no")

    with pytest.raises(Boom):
        _call_model(ModelConfig(model_id="m", should_cancel=lambda: False), client, [])


def test_cancel_ends_the_run_as_aborted_not_failed():
    ledger = Ledger.in_memory("task-cancel")
    runtime = _runtime(ledger)
    runtime.request_cancel()

    result = runtime.run(
        TaskSpec(task_id="task-cancel", objective="inspect", workspace="."),
        __import__("tests.kernel.test_north_star", fromlist=["ScriptedStrategy"]).ScriptedStrategy([]),
    )

    assert result.phase is TaskPhase.ABORTED
    reasons = [
        event.payload.get("reason")
        for event in ledger.read()
        if event.kind is EventKind.STATE_TRANSITION
    ]
    assert "cancelled" in reasons


def test_model_cancel_is_reported_as_its_own_reason():
    """`model call cancelled` and `cancelled` must stay distinguishable."""

    from tests.kernel.test_north_star import ScriptedStrategy

    ledger = Ledger.in_memory("task-cancel")
    runtime = _runtime(ledger)

    class Cancelling(ScriptedStrategy):
        def propose(self, view):
            raise ModelCancelled("model call cancelled by the operator")

    result = runtime.run(
        TaskSpec(task_id="task-cancel", objective="inspect", workspace="."),
        Cancelling([]),
    )

    assert result.phase is TaskPhase.ABORTED
    reasons = [
        event.payload.get("reason")
        for event in ledger.read()
        if event.kind is EventKind.STATE_TRANSITION
    ]
    assert "model call cancelled" in reasons


def test_cancel_is_not_a_budget_block():
    from tests.kernel.test_north_star import ScriptedStrategy

    ledger = Ledger.in_memory("task-cancel")
    runtime = _runtime(ledger)

    class Cancelling(ScriptedStrategy):
        def propose(self, view):
            raise ModelCancelled("stop")

    runtime.run(
        TaskSpec(task_id="task-cancel", objective="inspect", workspace="."),
        Cancelling([]),
    )

    assert [
        e for e in ledger.read() if e.kind is EventKind.POLICY_DECISION
    ] == [], "a cancel must not masquerade as a policy veto"
