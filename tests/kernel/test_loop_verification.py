"""Loop verification (#369): teach the agent once before killing the run.

Acceptance, each locked by one test:

1. a hit writes ``LOOP_DETECTED`` (kind/detail/tool) instead of going straight
   to ``blocked``;
2. the next view's ``Notes`` carries the diagnosis;
3. the model that changes approach continues (scripted: 4 same-failure hits
   that get taught, then a different wall that succeeds);
4. a same-signal repeat after teaching kills the run with the correction count
   in the reason;
5. ``max_loop_corrections=0`` behaves exactly like before (detect -> block).
"""

from __future__ import annotations

from tests.kernel.test_north_star import ScriptedStrategy, _runtime
from xenon.kernel.contracts import (
    CallTool,
    Capability,
    ReceiptStatus,
    TaskSpec,
    ToolCall,
    ToolIntent,
    ToolReceipt,
)
from xenon.kernel.loop_signals import StuckDetector
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


class _Wall:
    """A gateway that keeps failing with the same error text."""

    def __init__(self, error: str = "dir /b : Cannot find path") -> None:
        self._error = error
        self.calls = 0

    def execute(self, call: ToolCall, services) -> ToolReceipt:
        self.calls += 1
        return ToolReceipt(
            receipt_id=f"receipt-{self.calls}",
            call_id=f"call-{self.calls}",
            task_id="t",
            tool=call.spec_name,
            status=ReceiptStatus.FAILED,
            error=self._error,
            effects=(),
            declared_effects=(),
            observed_effects=(),
        )


def _read(path: str = "a.py") -> CallTool:
    return CallTool(
        ToolIntent(capability=Capability.READ, params={"file_path": path})
    )


def _spec(task_id: str, *, max_loop_corrections: int = 1) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        objective="run it",
        workspace=".",
        max_loop_corrections=max_loop_corrections,
    )


def test_a_hit_writes_loop_detected_with_structured_facts() -> None:
    ledger = Ledger.in_memory("loop-detect")
    runtime = _runtime(ledger)
    runtime._gateway = _Wall()
    runtime._stuck = StuckDetector(max_same_failure=2, max_identical=50)

    # 4th hit of the same wall triggers the signal (failures > max_same_failure).
    result = runtime.run(
        _spec("loop-detect"),
        ScriptedStrategy([_read(f"{i}.py") for i in range(4)]),
    )

    hits = [
        event.payload
        for event in ledger.read()
        if event.kind is EventKind.LOOP_DETECTED
    ]
    assert len(hits) == 1
    assert hits[0]["kind"] == "repeated_failure"
    assert "Cannot find path" in hits[0]["detail"]
    assert hits[0]["tool"] == "inspect"  # FakeCatalog binds READ to "inspect"
    assert result.accepted is False  # blocked after the taught hit + repeat


def test_the_diagnosis_lands_in_the_next_view_notes() -> None:
    ledger = Ledger.in_memory("loop-notes")
    runtime = _runtime(ledger)
    runtime._gateway = _Wall()
    runtime._stuck = StuckDetector(max_same_failure=2, max_identical=50)

    seen_notes: list[tuple[str, ...]] = []

    class Spy:
        def __init__(self, actions):
            self.actions = list(actions)

        def propose(self, view):
            seen_notes.append(tuple(getattr(view, "notes", ()) or ()))
            if not self.actions:
                return CallTool(
                    ToolIntent(capability=Capability.READ, params={"file_path": "x"})
                )
            return self.actions.pop(0)

    runtime.run(_spec("loop-notes"), Spy([_read(f"{i}.py") for i in range(4)]))

    assert any("Loop detected" in " ".join(notes) for notes in seen_notes)


def test_a_model_that_changes_approach_continues() -> None:
    ledger = Ledger.in_memory("loop-continue")
    runtime = _runtime(ledger)
    runtime._stuck = StuckDetector(max_same_failure=2, max_identical=50)

    class Gateway:
        def __init__(self):
            self.calls = 0

        def execute(self, call: ToolCall, services) -> ToolReceipt:
            self.calls += 1
            if self.calls <= 4:
                return ToolReceipt(
                    receipt_id=f"r-{self.calls}",
                    call_id=f"c-{self.calls}",
                    task_id="t",
                    tool=call.spec_name,
                    status=ReceiptStatus.FAILED,
                    error="dir /b : Cannot find path",
                    effects=(),
                    declared_effects=(),
                    observed_effects=(),
                )
            # The model changed approach: a different tool succeeds.
            return ToolReceipt(
                receipt_id=f"r-{self.calls}",
                call_id=f"c-{self.calls}",
                task_id="t",
                tool="check",
                status=ReceiptStatus.OK,
                effects=(),
                declared_effects=(),
                observed_effects=(),
            )

    runtime._gateway = Gateway()
    from xenon.kernel.contracts import Finish

    actions = [_read(f"{i}.py") for i in range(4)] + [
        CallTool(ToolIntent(capability=Capability.VERIFY)),
        Finish("done"),
    ]

    result = runtime.run(_spec("loop-continue"), ScriptedStrategy(actions))

    # Not blocked: the run got taught, changed approach and was allowed to
    # finish.  (It finishes unverified here — the point is that the loop
    # detector did not kill it.)
    assert result.phase.value != "blocked"


def test_a_same_signal_repeat_after_teaching_kills_the_run() -> None:
    ledger = Ledger.in_memory("loop-repeat")
    runtime = _runtime(ledger)
    runtime._gateway = _Wall()
    runtime._stuck = StuckDetector(max_same_failure=2, max_identical=50)

    # Hits 1..4: the 4th hit teaches (attempt 0).  Hit 5 is the same signal
    # again and must kill the run with the correction count in the reason.
    result = runtime.run(
        _spec("loop-repeat"),
        ScriptedStrategy([_read(f"{i}.py") for i in range(5)]),
    )

    assert result.accepted is False
    reasons = [
        event.payload.get("reason", "")
        for event in ledger.read()
        if event.kind is EventKind.POLICY_DECISION
    ]
    assert any("after 1 correction attempt" in reason for reason in reasons), reasons


def test_max_loop_corrections_zero_blocks_immediately() -> None:
    ledger = Ledger.in_memory("loop-zero")
    runtime = _runtime(ledger)
    runtime._gateway = _Wall()
    runtime._stuck = StuckDetector(max_same_failure=2, max_identical=50)

    result = runtime.run(
        _spec("loop-zero", max_loop_corrections=0),
        ScriptedStrategy([_read(f"{i}.py") for i in range(4)]),
    )

    assert result.accepted is False
    hits = [
        event for event in ledger.read() if event.kind is EventKind.LOOP_DETECTED
    ]
    notes = [
        event.payload
        for event in ledger.read()
        if event.kind is EventKind.POLICY_DECISION
    ]
    # No teaching happened: the first hit blocked straight away.
    assert len(hits) == 1
    assert len(notes) == 1
    assert "0 correction attempt" not in notes[0]["reason"]


def test_a_negative_max_loop_corrections_is_rejected_at_the_boundary() -> None:
    import pytest

    with pytest.raises(ValueError):
        _spec("bad", max_loop_corrections=-1)


def test_loop_notes_stay_bounded_under_repeated_teaching() -> None:
    """#369 review: a run that keeps changing walls must not blow the Notes."""
    from tests.kernel.test_north_star import _runtime
    from xenon.kernel.contracts import Abort

    ledger = Ledger.in_memory("notes-bounded")
    runtime = _runtime(ledger)
    runtime._gateway = _Wall()

    class Cycle:
        def __init__(self):
            self.proposals = 0

        def propose(self, view):
            self.proposals += 1
            if self.proposals > 12:
                return Abort("done testing")
            return _read(f"{self.proposals}.py")

    result = runtime.run(_spec("notes-bounded"), Cycle())

    # 12 proposals teach up to 12 loop notes; the view must stay bounded.
    assert len(runtime._loop_notes) <= 5
    assert result.accepted is False or result.phase.value in {
        "blocked", "aborted"
    }
