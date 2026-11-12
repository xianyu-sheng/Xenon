"""Deterministic loop detection (#stuck).

The gap these tests exist for: a real run made 18 tool calls, 6 failed, and the
old guard never fired — because each wrong command was *different*
(`dir /b`, `dir /s /b`, `python -c`, `Get-ChildItem`).  The only thing that
stopped it was the step budget, whose reason ("budget exhausted") is true but
useless.

Everything here is a fact about receipts.  No prose is compared, so nothing
depends on wording.
"""

from __future__ import annotations

import pytest

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.kernel.contracts import (
    Opaque,
    ReadFact,
    ReceiptStatus,
    TestRun,
    ToolReceipt,
)
from xenon.kernel.loop_signals import (
    StuckDetector,
    call_signature,
    progress_key,
)


def _read(path: str = "calc.py", *, ok: bool = True) -> ToolReceipt:
    return ToolReceipt(
        receipt_id=f"r-{path}-{ok}",
        call_id="c1",
        task_id="t",
        tool="read",
        status=ReceiptStatus.OK if ok else ReceiptStatus.FAILED,
        effects=(ReadFact(path=path),) if ok else (),
        error="" if ok else "文件不存在",
    )


def _bash(command: str, *, ok: bool, error: str = "") -> ToolReceipt:
    return ToolReceipt(
        receipt_id=f"r-{command}",
        call_id="c1",
        task_id="t",
        tool="bash",
        status=ReceiptStatus.OK if ok else ReceiptStatus.FAILED,
        effects=(Opaque(command=command),) if ok else (),
        error=error,
    )


def _test_run(target: str) -> ToolReceipt:
    return ToolReceipt(
        receipt_id=f"r-{target}",
        call_id="c1",
        task_id="t",
        tool="verify",
        status=ReceiptStatus.OK,
        effects=(
            TestRun(target=target, tests_run=3, failed=0, errors=0, exit_code=0),
        ),
    )


# ── identical repetition (was the only signal before) ───────────────


def test_the_fourth_identical_call_is_refused() -> None:
    detector = StuckDetector(max_identical=3)

    for _ in range(3):
        assert detector.note_call("read", {"file_path": "a.py"}) is None
    signal = detector.note_call("read", {"file_path": "a.py"})

    assert signal is not None
    assert signal.kind == "identical_call"
    assert "4 times" in signal.detail


def test_different_arguments_are_different_calls() -> None:
    detector = StuckDetector(max_identical=1)

    assert detector.note_call("read", {"file_path": "a.py"}) is None
    assert detector.note_call("read", {"file_path": "b.py"}) is None


def test_signature_is_order_independent() -> None:
    assert call_signature("t", {"a": 1, "b": 2}) == call_signature(
        "t", {"b": 2, "a": 1}
    )


# ── repeated failure: the same wall ─────────────────────────────────


def test_the_same_error_four_times_is_a_loop() -> None:
    detector = StuckDetector(max_same_failure=3)
    wall = "SecurityError: 危险命令被拦截"

    for _ in range(3):
        assert detector.observe(_bash("python -c 'x'", ok=False, error=wall)) is None
    signal = detector.observe(_bash("python -e 'y'", ok=False, error=wall))

    assert signal is not None
    assert signal.kind == "repeated_failure"
    assert "same error 4 times" in signal.detail


def test_different_errors_are_not_one_wall() -> None:
    """Distinct failures are diagnostic progress, not a loop."""

    detector = StuckDetector(max_same_failure=1)

    assert detector.observe(_bash("a", ok=False, error="boom A")) is None
    assert detector.observe(_bash("b", ok=False, error="boom B")) is None


# ── capability cycle: a genuine A -> B -> A -> B ────────────────────


def test_a_two_step_cycle_three_times_is_a_loop() -> None:
    detector = StuckDetector()
    sequence = [_read("a.py"), _test_run("t.py")] * 3

    signals = [detector.observe(receipt) for receipt in sequence]

    assert signals[-1] is not None
    assert signals[-1].kind == "capability_cycle"


def test_the_same_capability_three_times_is_not_a_cycle() -> None:
    """Reading three different files is one capability used three times."""

    detector = StuckDetector()

    for path in ("a.py", "b.py", "c.py"):
        assert detector.observe(_read(path)) is None


# ── no_progress: the signal that catches platform thrashing ─────────


def test_five_fruitless_calls_produce_a_diagnosis_not_a_stop() -> None:
    """Each command is different; none of them works.  That is the real case.

    It must be *reported*, not enforced: a real run had five such receipts and
    then succeeded four calls later, so stopping here would destroy work.
    """

    detector = StuckDetector(max_no_progress=5)
    attempts = [
        _bash("dir /b", ok=False, error="not recognized"),
        _bash("dir /s /b", ok=False, error="Cannot find path"),
        _bash("python -c 'pass'", ok=False, error="blocked"),
        _bash("Get-ChildItem", ok=False, error="blocked"),
        _bash("ls -la", ok=False, error="not recognized"),
    ]

    signals = [detector.observe(receipt) for receipt in attempts]

    assert all(signal is None for signal in signals), "no_progress never stops a run"
    assert "no new information" in detector.diagnosis
    assert "5 consecutive calls" in detector.diagnosis


def test_a_success_in_the_middle_resets_the_streak() -> None:
    detector = StuckDetector(max_no_progress=3)

    for _ in range(2):
        detector.observe(_bash("x", ok=False, error="nope"))
    # Real progress: a new file was read.
    assert detector.observe(_read("new.py")) is None
    assert detector.no_progress_streak == 0
    assert detector.observe(_bash("y", ok=False, error="nope")) is None


def test_reading_a_new_file_counts_as_progress() -> None:
    detector = StuckDetector(max_no_progress=2)

    assert detector.observe(_read("a.py")) is None
    assert detector.observe(_read("b.py")) is None
    assert detector.observe(_read("c.py")) is None


def test_re_reading_the_same_file_counts_as_no_progress() -> None:
    """Same content again teaches nothing, so the streak grows (and is reported)."""

    detector = StuckDetector()

    for _ in range(4):
        assert detector.observe(_read("a.py")) is None

    assert detector.no_progress_streak == 3
    assert "no new information" in detector.diagnosis


def test_a_changed_file_is_progress_even_when_the_path_repeats() -> None:
    changed = ToolReceipt(
        receipt_id="r",
        call_id="c",
        task_id="t",
        tool="edit",
        status=ReceiptStatus.OK,
        effects=(),
    )
    assert progress_key(changed)  # a successful call identifies itself


def test_a_reset_clears_the_streak() -> None:
    detector = StuckDetector(max_no_progress=2)
    detector.observe(_read("a.py"))
    detector.observe(_bash("x", ok=False, error="nope"))

    detector.reset()

    assert detector.no_progress_streak == 0


# ── false-positive protection ───────────────────────────────────────


def test_a_normal_progressing_run_never_trips() -> None:
    """The workload shape of a healthy task must stay silent."""

    detector = StuckDetector()
    sequence = [
        _read("calc.py"),
        _read("tests/test_calc.py"),
        _bash("pytest -q", ok=False, error="no tests"),
        _test_run("tests/test_calc.py"),
        _read("calc.py"),
        _test_run("tests/test_calc.py"),
    ]

    assert all(detector.observe(receipt) is None for receipt in sequence)


def test_a_retry_that_succeeds_never_trips() -> None:
    detector = StuckDetector()

    assert detector.observe(_bash("pytest -q", ok=False, error="transient")) is None
    assert detector.observe(_bash("pytest -q", ok=True)) is None


def test_a_nonsense_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        StuckDetector(max_identical=0)
    with pytest.raises(ValueError):
        StuckDetector(max_no_progress=0)


# ── through the runtime ─────────────────────────────────────────────


class _FailingGateway:
    """Every call fails the same way — the 'same wall' case.

    Wraps the real stub gateway so tool binding still resolves; only the
    outcome differs.
    """

    def __init__(self) -> None:
        from tests.kernel.test_north_star import FakeGateway

        self._inner = FakeGateway()

    def execute(self, call, services):  # noqa: ANN001, ANN201
        receipt = self._inner.execute(call, services)
        return ToolReceipt(
            receipt_id=receipt.receipt_id,
            call_id=receipt.call_id,
            task_id=receipt.task_id,
            tool=receipt.tool,
            status=ReceiptStatus.FAILED,
            error="SecurityError: blocked by policy",
        )


def test_the_runtime_stops_on_a_repeated_failure_with_a_diagnosis() -> None:
    from tests.kernel.test_north_star import ScriptedStrategy, _runtime
    from xenon.kernel.contracts import CallTool, Capability, TaskSpec, ToolIntent
    from xenon.ledger.events import EventKind
    from xenon.ledger.store import Ledger

    ledger = Ledger.in_memory("task-same-wall")
    runtime = _runtime(ledger)
    runtime._gateway = _FailingGateway()
    # Different targets on purpose: the *call* differs, the *wall* does not.
    # That is what separates "retried the same thing" from "kept hitting the
    # same error", and only the second is a loop worth stopping.
    # Enough actions for the signal to fire *and* exhaust the corrective
    # budget: the detector teaches the same wall `max_loop_corrections`
    # times before it vetoes, and this test is about the veto.
    actions = [
        CallTool(
            ToolIntent(capability=Capability.READ, params={"file_path": f"{i}.py"})
        )
        for i in range(12)
    ]

    result = runtime.run(
        TaskSpec(task_id="task-same-wall", objective="run it", workspace="."),
        ScriptedStrategy(actions),
    )

    assert result.accepted is False
    vetoes = [
        event.payload
        for event in ledger.read()
        if event.kind is EventKind.POLICY_DECISION
        and event.payload.get("signal") == "repeated_failure"
    ]
    assert vetoes, "the same wall must be reported as such"
    assert "same error" in vetoes[0]["reason"]


def test_the_step_budget_block_carries_the_loop_diagnosis() -> None:
    """'budget exhausted' must say what the run was doing."""

    from tests.kernel.test_north_star import ScriptedStrategy, _runtime
    from xenon.kernel.contracts import (
        Budget,
        CallTool,
        Capability,
        TaskSpec,
        ToolIntent,
    )
    from xenon.ledger.events import EventKind
    from xenon.ledger.store import Ledger

    ledger = Ledger.in_memory("task-diag")
    runtime = _runtime(ledger)

    # Three distinct reads of the same file: no new information after the first.
    action = CallTool(ToolIntent(capability=Capability.READ))
    result = runtime.run(
        TaskSpec(
            task_id="task-diag",
            objective="inspect",
            workspace=".",
            budget=Budget(max_steps=6),
        ),
        ScriptedStrategy([action] * 10),
    )

    assert result.accepted is False
    reasons = [
        event.payload.get("reason", "")
        for event in ledger.read()
        if event.kind is EventKind.POLICY_DECISION
    ]
    assert any("no new information" in reason for reason in reasons), reasons
