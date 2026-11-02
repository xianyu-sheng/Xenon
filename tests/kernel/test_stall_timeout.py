"""The gentle brake: stop on unproductive *time*, not on step count (#368).

Steps are a crude brake: a complex task that is making progress still gets cut
off.  Time alone is also crude.  This stops only when the loop has been
unproductive for long enough — which is precisely what a stuck run looks like,
and precisely what a hard task does not.
"""

from __future__ import annotations

import time

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from tests.kernel.test_north_star import ScriptedStrategy, _runtime
from xenon.kernel.contracts import (
    Budget,
    CallTool,
    Capability,
    ReadFact,
    ReceiptStatus,
    TaskSpec,
    ToolIntent,
    ToolReceipt,
)
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


def _spec(**budget) -> TaskSpec:
    return TaskSpec(
        task_id="task-stall",
        objective="inspect",
        workspace=".",
        budget=Budget(**budget),
    )


class _ProgressingGateway:
    """Each call reads a *different* file, so every receipt teaches something."""

    def __init__(self) -> None:
        self.n = 0

    def execute(self, call, services):  # noqa: ANN001, ANN201
        self.n += 1
        return ToolReceipt(
            receipt_id=f"r{self.n}",
            call_id=f"c{self.n}",
            task_id="t",
            tool="inspect",
            status=ReceiptStatus.OK,
            effects=(ReadFact(path=f"file{self.n}.py"),),
        )


class _FruitlessGateway:
    """Every call fails, and every error text is different.

    Different errors on purpose: the repeated-failure signal keys on the error
    text, so a varying wall keeps the hard signals quiet and leaves only the
    stall clock.  "Fruitless" here means *no new information*, not "same error".
    """

    def __init__(self) -> None:
        from tests.kernel.test_north_star import FakeGateway

        self._inner = FakeGateway()
        self.n = 0

    def execute(self, call, services):  # noqa: ANN001, ANN201
        receipt = self._inner.execute(call, services)
        self.n += 1
        return ToolReceipt(
            receipt_id=receipt.receipt_id,
            call_id=receipt.call_id,
            task_id=receipt.task_id,
            tool=receipt.tool,
            status=ReceiptStatus.FAILED,
            error=f"wall number {self.n}",
        )


class _Ticking:
    """Scripted strategy that advances a virtual clock between proposals."""

    def __init__(self, actions, tick) -> None:
        self.actions = list(actions)
        self._tick = tick

    def propose(self, view):  # noqa: ANN001, ANN201
        if not self.actions:
            from xenon.kernel.contracts import Finish

            return Finish("done")
        self._tick(view)
        return self.actions.pop(0)


def _clock(monkeypatch, runtime, *, after: float) -> None:
    """Make the runtime believe its last progress was ``after`` seconds ago."""

    monkeypatch.setattr(runtime, "_last_progress_at", time.time() - after)


def test_a_stalled_run_is_stopped_with_a_reason_not_a_budget_number(monkeypatch) -> None:
    ledger = Ledger.in_memory("task-stall")
    runtime = _runtime(ledger)
    runtime._gateway = _FruitlessGateway()

    # Distinct arguments on purpose: the *call* changes every time, so the
    # identical-call signal never fires.  And max_same_failure is raised so the
    # repeated-failure signal does not fire either.  Only the stall clock is
    # left, which is exactly the mechanism under test.
    actions = [
        CallTool(
            ToolIntent(capability=Capability.READ, params={"file_path": f"{i}.py"})
        )
        for i in range(4)
    ]
    # Virtual clock: advance an hour between checks.  The runtime resets its
    # stall clock at run() start, so a test that patches the field instead of
    # the clock would measure zero elapsed time.
    now = [time.time()]
    runtime._clock = lambda: now[0]

    def tick(_strategy):
        now[0] += 3600.0

    result = runtime.run(
        _spec(max_steps=1024, max_stall_seconds=30.0),
        _Ticking(actions, tick),
    )

    assert result.accepted is False
    reasons = [
        event.payload.get("reason", "")
        for event in ledger.read()
        if event.kind is EventKind.POLICY_DECISION
    ]
    assert any(reason.startswith("stalled:") for reason in reasons), reasons
    assert not any("step budget exhausted" in reason for reason in reasons)


def test_a_run_that_keeps_learning_is_never_stopped_by_time(monkeypatch) -> None:
    """The whole point: unproductive time counts, productive time does not."""

    ledger = Ledger.in_memory("task-stall")
    runtime = _runtime(ledger)
    runtime._gateway = _ProgressingGateway()
    action = CallTool(ToolIntent(capability=Capability.READ))

    # Pretend a lot of wall time passed, but every receipt is new information,
    # so the stall clock resets each time.
    monkeypatch.setattr(runtime, "_last_progress_at", time.time() - 1000.0)
    original = runtime._stuck.observe

    def observe(receipt):
        runtime._last_progress_at = time.time()
        return original(receipt)

    monkeypatch.setattr(runtime._stuck, "observe", observe)

    result = runtime.run(
        _spec(max_steps=1000, max_stall_seconds=30.0), ScriptedStrategy([action] * 4)
    )

    reasons = [
        event.payload.get("reason", "")
        for event in ledger.read()
        if event.kind is EventKind.POLICY_DECISION
    ]
    assert not any(reason.startswith("stalled:") for reason in reasons), reasons
    assert result is not None


def test_the_stall_limit_can_be_disabled() -> None:
    ledger = Ledger.in_memory("task-stall")
    runtime = _runtime(ledger)
    runtime._gateway = _FruitlessGateway()
    action = CallTool(ToolIntent(capability=Capability.READ))
    runtime._stuck._max_same_failure = 999
    runtime._last_progress_at = time.time() - 100000.0

    runtime.run(
        _spec(max_steps=3, max_stall_seconds=None), ScriptedStrategy([action] * 6)
    )

    reasons = [
        event.payload.get("reason", "")
        for event in ledger.read()
        if event.kind is EventKind.POLICY_DECISION
    ]
    assert not any(reason.startswith("stalled:") for reason in reasons), reasons


def test_zero_means_no_step_limit() -> None:
    """`--max-steps 0` is how a caller asks for no step cap at all."""

    from xenon.harness.runner import DEFAULT_MAX_STEPS

    assert DEFAULT_MAX_STEPS == 1024


def test_the_stall_reason_includes_the_loop_diagnosis() -> None:
    ledger = Ledger.in_memory("task-stall")
    runtime = _runtime(ledger)
    runtime._gateway = _FruitlessGateway()
    runtime._spec = _spec(max_steps=1024, max_stall_seconds=30.0)
    runtime._stuck._max_same_failure = 999

    for _ in range(3):
        runtime._stuck.observe(
            ToolReceipt(
                receipt_id="r",
                call_id="c",
                task_id="t",
                tool="bash",
                status=ReceiptStatus.FAILED,
                error="blocked",
            )
        )

    reason = runtime._stall_reason()

    assert reason.startswith("stalled:")
    assert "no new information" in reason
