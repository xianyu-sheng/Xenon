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

    assert _call_model(ModelConfig(model_id="test/fake"), client, []) == "ok"
    assert seen == ["test/fake"]


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
        _call_model(ModelConfig(model_id="test/fake", should_cancel=lambda: False), client, [])


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


# ── 进度可见（kernel 流式）─────────────────────────────────────────


def test_progress_hook_reports_elapsed_and_size():
    """Kernel output is a protocol payload, so show progress, not prose."""

    import time as _time

    ticks: list[tuple[float, int]] = []

    def slow_client(model_id, messages, **kwargs):
        _time.sleep(0.5)
        return '{"action":"think","text":"ok"}'

    result = _call_model(
        ModelConfig(model_id="test/fake", progress_sink=lambda s, n: ticks.append((s, n))),
        slow_client,
        [],
    )

    assert result.startswith('{"action"')
    assert ticks, "the waiting thread must see progress"
    assert ticks[0][0] >= 0.0


def test_streaming_accumulates_chunks(monkeypatch):
    """With a progress hook the reply is assembled from stream chunks."""

    import xenon.harness.llm as llm_mod

    def fake_stream(model_id, messages, **kwargs):
        import time as _time

        for part in ('{"action":', '"think",', '"text":"hi"}'):
            # Slow enough that the waiting thread observes progress: the poll
            # interval is 150ms, so each chunk must outlast one tick.
            _time.sleep(0.25)
            yield part

    monkeypatch.setattr(
        "xenon.utils.llm_client.chat_completion_stream", fake_stream
    )
    ticks: list[tuple[float, int]] = []

    result = _call_model(
        ModelConfig(model_id="test/fake", progress_sink=lambda s, n: ticks.append((s, n))),
        None,
        [{"role": "user", "content": "hi"}],
    )

    assert result == '{"action":"think","text":"hi"}'
    assert ticks, "chunks must be visible while they arrive"
    assert llm_mod  # module import stays exercised


def test_no_hook_means_no_streaming_and_no_thread():
    calls: list[str] = []

    def client(model_id, messages, **kwargs):
        calls.append("called")
        return "ok"

    assert _call_model(ModelConfig(model_id="test/fake"), client, []) == "ok"
    assert calls == ["called"]


def test_progress_failure_does_not_break_the_call():
    def boom(seconds, chars):
        raise RuntimeError("ui died")

    def slow_client(model_id, messages, **kwargs):
        import time as _time

        _time.sleep(0.3)
        return "ok"

    assert (
        _call_model(ModelConfig(model_id="test/fake", progress_sink=boom), slow_client, [])
        == "ok"
    )


def test_progress_reporter_stays_quiet_until_a_call_looks_stuck():
    """#403: per-second narration pushed the real answer off screen.

    A 19-step turn printed "模型生成中 Ns · M 字符" hundreds of times.  The
    status bar already carries the live counter, so the console gets at most
    one notice per model call, and only after the call is slow enough that
    silence would read as a hang.
    """

    from types import SimpleNamespace

    from xenon.repl.runtime_adapter import kernel_progress_reporter

    printed: list[str] = []

    class _Console:
        def print(self, message, **kwargs):
            printed.append(str(message))

    repl = SimpleNamespace(console=_Console())
    report = kernel_progress_reporter(repl)

    for seconds in (0.2, 1.0, 2.5, 9.0, 14.9):
        report(seconds, 900)
    assert printed == [], "a call under the threshold must not narrate"

    report(15.0, 1200)
    assert len(printed) == 1 and "仍在生成" in printed[0]

    for seconds in (16.0, 17.0, 25.0):
        report(seconds, 3000)
    assert len(printed) == 1, "one notice per call, not one per tick"

    # The clock restarts on a new model call: a slow call still gets one line.
    report(1.0, 10)
    report(15.5, 800)
    assert len(printed) == 2
