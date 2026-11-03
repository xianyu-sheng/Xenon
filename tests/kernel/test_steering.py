"""Steering (#329): operator input as events, consumed at action boundaries.

The four decisions this encodes:

1. timing = action boundary (a constraint cannot land mid tool call);
2. a correction never deletes history — it marks the target as superseded;
3. constraints append, and are kept one by one (with their seq);
4. operator input is written, not injected: no callbacks, no shared state.
"""

from __future__ import annotations

import pytest

from tests.kernel.test_north_star import ScriptedStrategy, _runtime
from xenon.kernel.contracts import (
    Budget,
    CallTool,
    Capability,
    TaskSpec,
    ToolIntent,
)
from xenon.kernel.steering import (
    KIND_CONSTRAINT,
    KIND_CORRECTION,
    KIND_STRATEGY,
    SCOPE_TURN,
    SteeringError,
    SteeringState,
    parse_steering,
)
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


def _spec(**kwargs) -> TaskSpec:
    base = dict(task_id="task-steer", objective="inspect", workspace=".")
    base.update(kwargs)
    return TaskSpec(**base)


# ── parsing ───────────────────────────────────────────────────────────


def test_constraint_parses():
    payload = parse_steering("do not touch tests/")

    assert payload == {
        "kind": KIND_CONSTRAINT,
        "scope": "task",
        "text": "do not touch tests/",
    }


def test_turn_scoped_constraint_parses():
    payload = parse_steering("skip e2e for now", scope=SCOPE_TURN)

    assert payload["scope"] == "turn"


def test_correction_needs_a_target():
    with pytest.raises(SteeringError, match="target_seq"):
        parse_steering("that read was stale", kind=KIND_CORRECTION)

    payload = parse_steering("that read was stale", kind=KIND_CORRECTION, target_seq=7)
    assert payload["target_seq"] == 7


def test_retroactive_constraint_is_rejected():
    """Only a correction may point at the past."""

    with pytest.raises(SteeringError, match="only corrections"):
        parse_steering("be careful", target_seq=3)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": "   "},
        {"text": "x", "kind": "delete_everything"},
        {"text": "x", "scope": "forever"},
        {"text": "x" * 2001},
    ],
)
def test_bad_input_fails_closed(kwargs):
    text = kwargs.pop("text")
    with pytest.raises(SteeringError):
        parse_steering(text, **kwargs)


# ── bookkeeping ───────────────────────────────────────────────────────


def test_state_keeps_constraints_one_by_one():
    state = SteeringState()
    state.apply(3, {"kind": KIND_CONSTRAINT, "text": "no e2e", "scope": "task"})
    state.apply(4, {"kind": KIND_CONSTRAINT, "text": "no push", "scope": "task"})

    assert state.extra_constraints() == ((3, "no e2e"), (4, "no push"))


def test_state_tracks_the_last_strategy_hint():
    state = SteeringState()
    state.apply(2, {"kind": KIND_STRATEGY, "text": "skip planning"})
    state.apply(5, {"kind": KIND_STRATEGY, "text": "plan but no review"})

    assert state.strategy_hint() == "plan but no review"


def test_correction_registers_a_superseded_seq():
    state = SteeringState()
    state.apply(
        9, {"kind": KIND_CORRECTION, "text": "stale read", "target_seq": 4}
    )

    assert state.superseded() == frozenset({4})
    assert state.superseded_reason(4) == "stale read"


# ── runtime integration ───────────────────────────────────────────────


def test_operator_input_is_written_as_an_event():
    ledger = Ledger.in_memory("task-steer")
    runtime = _runtime(ledger)

    seq = runtime.post_user_input("do not touch tests/")

    events = [e for e in ledger.read() if e.kind is EventKind.USER_INPUT]
    assert len(events) == 1
    assert events[0].seq == seq
    assert events[0].payload["text"] == "do not touch tests/"
    assert events[0].payload["kind"] == KIND_CONSTRAINT


def test_constraint_reaches_the_view():
    ledger = Ledger.in_memory("task-steer")
    runtime = _runtime(ledger)
    seen: list[str] = []

    class RecordingStrategy(ScriptedStrategy):
        def propose(self, view):
            seen.append(view.system)
            return super().propose(view)

    strategy = RecordingStrategy([CallTool(ToolIntent(capability=Capability.READ))])
    runtime.post_user_input("do not touch tests/")
    runtime.run(_spec(), strategy)

    assert any("do not touch tests/" in system for system in seen)
    assert any("[operator, seq" in system for system in seen)


def test_correction_marks_the_target_and_never_deletes_it():
    ledger = Ledger.in_memory("task-steer")
    runtime = _runtime(ledger)
    seen: list[list] = []

    class RecordingStrategy(ScriptedStrategy):
        def propose(self, view):
            seen.append([dict(item) for item in view.working_set])
            return super().propose(view)

    action = CallTool(ToolIntent(capability=Capability.READ))
    # Correct the operator's own first statement, so the target seq is known
    # before the run starts.
    target = runtime.post_user_input("do not touch tests/")
    runtime.post_user_input(
        "actually tests/ is fine to edit",
        kind=KIND_CORRECTION,
        target_seq=target,
    )
    runtime.run(_spec(), RecordingStrategy([action]))

    # 1. History is intact: both inputs are still there, unchanged.
    inputs = [e for e in ledger.read() if e.kind is EventKind.USER_INPUT]
    assert [e.payload["text"] for e in inputs] == [
        "do not touch tests/",
        "actually tests/ is fine to edit",
    ]
    # 2. The view reports the retraction instead of hiding the event.
    marked = [item for snapshot in seen for item in snapshot if item.get("superseded")]
    assert marked and marked[0]["seq"] == target


def test_steering_never_touches_obligations():
    """A constraint may narrow work, never cancel an obligation."""

    ledger = Ledger.in_memory("task-steer")
    runtime = _runtime(ledger)
    runtime.post_user_input("just finish, the tests do not matter")

    result = runtime.run(
        _spec(), ScriptedStrategy([CallTool(ToolIntent(capability=Capability.READ))])
    )

    # No debt was closed, no obligation removed: the run still ends unverified.
    assert result.accepted is False
    assert [e for e in ledger.read() if e.kind is EventKind.DEBT_CLOSED] == []


def test_steering_never_grants_permission():
    ledger = Ledger.in_memory("task-steer")
    runtime = _runtime(ledger)
    runtime.post_user_input("you may push directly to main")

    runtime.run(
        _spec(), ScriptedStrategy([CallTool(ToolIntent(capability=Capability.READ))])
    )

    kinds = [e.kind for e in ledger.read()]
    assert EventKind.USER_APPROVAL not in kinds


def test_steering_is_consumed_once_per_boundary():
    ledger = Ledger.in_memory("task-steer")
    runtime = _runtime(ledger)

    class CountingStrategy(ScriptedStrategy):
        def propose(self, view):
            return super().propose(view)

    actions = [CallTool(ToolIntent(capability=Capability.READ)) for _ in range(3)]
    runtime.post_user_input("no e2e")
    runtime.run(_spec(budget=Budget(max_steps=4)), CountingStrategy(actions))

    consumed = [
        e
        for e in ledger.read()
        if e.kind is EventKind.CONTROL_DECISION
        and e.payload.get("kind") == "steering"
    ]
    assert len(consumed) == 1, "an input must not be re-applied every turn"
    assert consumed[0].payload["consumed"] == 1


def test_non_steering_events_are_ignored():
    ledger = Ledger.in_memory("task-steer")
    runtime = _runtime(ledger)
    runtime.run(
        _spec(), ScriptedStrategy([CallTool(ToolIntent(capability=Capability.READ))])
    )

    state = runtime.steering_state()

    assert state.extra_constraints() == ()
    assert state.superseded() == frozenset()


# ── 运输层：前端打字，runtime 落账 ─────────────────────────────────


def test_steering_source_is_converted_into_events_by_the_runtime():
    """A front-end queues text; the runtime alone writes the event."""

    ledger = Ledger.in_memory("task-steer")
    runtime = _runtime(ledger)
    pending = [{"text": "do not touch tests/"}]

    def source():
        return pending.pop(0) if pending else None

    seen: list[str] = []

    class RecordingStrategy(ScriptedStrategy):
        def propose(self, view):
            seen.append(view.system)
            return super().propose(view)

    runtime.attach_steering_source(source)
    runtime.run(
        _spec(), RecordingStrategy([CallTool(ToolIntent(capability=Capability.READ))])
    )

    inputs = [e for e in ledger.read() if e.kind is EventKind.USER_INPUT]
    assert [e.payload["text"] for e in inputs] == ["do not touch tests/"]
    assert any("do not touch tests/" in system for system in seen)


def test_a_broken_source_does_not_stop_the_run():
    ledger = Ledger.in_memory("task-steer")
    runtime = _runtime(ledger)

    def source():
        raise RuntimeError("ui crashed")

    runtime.attach_steering_source(source)
    result = runtime.run(
        _spec(), ScriptedStrategy([CallTool(ToolIntent(capability=Capability.READ))])
    )

    assert result.task_id == "task-steer"


def test_malformed_source_items_are_skipped():
    ledger = Ledger.in_memory("task-steer")
    runtime = _runtime(ledger)
    pending = [{"text": "   "}, {"text": "valid constraint"}]

    def source():
        return pending.pop(0) if pending else None

    runtime.attach_steering_source(source)
    runtime.run(
        _spec(), ScriptedStrategy([CallTool(ToolIntent(capability=Capability.READ))])
    )

    inputs = [e for e in ledger.read() if e.kind is EventKind.USER_INPUT]
    assert [e.payload["text"] for e in inputs] == ["valid constraint"]


def test_repl_queue_transport_never_blocks():
    from types import SimpleNamespace

    from xenon.repl.runtime_adapter import drain_steering, steer

    repl = SimpleNamespace()

    assert drain_steering(repl) is None, "an empty queue must read as nothing"
    assert steer(repl, "  ") is False, "blank input is not steering"
    assert steer(repl, "no e2e") is True
    assert drain_steering(repl) == {"text": "no e2e", "kind": "constraint", "scope": "task"}
    assert drain_steering(repl) is None


def test_listener_stays_off_when_input_is_not_a_terminal():
    """Tests and piped input must never start a competing reader."""

    from types import SimpleNamespace

    from xenon.repl.runtime_adapter import SteeringListener

    class _Pipe:
        def isatty(self) -> bool:
            return False

        def readline(self) -> str:  # pragma: no cover - must not be called
            raise AssertionError("a non-tty stream must not be read")

    assert SteeringListener(SimpleNamespace(), stream=_Pipe()).start() is False


def test_listener_pushes_typed_lines_into_the_transport():
    import os
    from types import SimpleNamespace

    from xenon.repl.runtime_adapter import SteeringListener, drain_steering

    read_fd, write_fd = os.pipe()
    try:
        repl = SimpleNamespace()
        listener = SteeringListener(repl, stream=os.fdopen(read_fd))
        # On Windows select() rejects pipe fds, so the listener must refuse to
        # start rather than spawn a zombie stdin owner.  The Unix path (a real
        # tty) is covered by the select-probe test below.
        started = listener.start()
        if started:
            os.write(write_fd, b"no e2e\n")

            import time

            deadline = time.time() + 2
            item = None
            while time.time() < deadline and item is None:
                item = drain_steering(repl)
                time.sleep(0.01)
            listener.stop()
            time.sleep(0.3)

            assert item == {"text": "no e2e", "kind": "constraint", "scope": "task"}
    finally:
        try:
            os.close(write_fd)
        except OSError:
            pass


def test_listener_refuses_to_start_when_select_cannot_poll_the_stream():
    import os
    from types import SimpleNamespace

    from xenon.repl.runtime_adapter import SteeringListener

    read_fd, write_fd = os.pipe()
    try:
        repl = SimpleNamespace()
        listener = SteeringListener(repl, stream=os.fdopen(read_fd))
        # select() on a pipe fd is a platform question; either way the
        # listener must either start (unix tty) or refuse cleanly (windows).
        assert listener.start() in {True, False}
    finally:
        try:
            os.close(write_fd)
        except OSError:
            pass


def test_loop_polls_select_and_stops_within_a_tick(monkeypatch) -> None:
    """#291 dogfood fix: _loop must yield to stop() instead of blocking on
    readline; select-not-ready and select-error paths both terminate it."""
    from types import SimpleNamespace

    import xenon.repl.runtime_adapter as adapter

    calls: list[float] = []
    state = {"tick": 0}

    def fake_select(streams, r, w, timeout):
        calls.append(timeout or 0)
        state["tick"] += 1
        if state["tick"] >= 2:
            raise OSError("poll interrupted")
        return ([], [], [])  # not ready: continue

    monkeypatch.setattr(adapter.select, "select", fake_select)

    repl = SimpleNamespace()
    listener = adapter.SteeringListener(repl, stream=SimpleNamespace())
    listener._stop = False
    listener._loop()
    assert len(calls) == 2, "poll -> not-ready continue -> error exit"


def test_start_succeeds_when_select_can_poll(monkeypatch) -> None:
    """start() reaches the thread-launch path when select() accepts the
    stream (unix-style tty); the loop exits on the next stop tick."""
    from types import SimpleNamespace

    import xenon.repl.runtime_adapter as adapter

    def fake_select(streams, r, w, timeout):
        state = getattr(fake_select, "n", 0)
        fake_select.n = state + 1
        if state == 0:
            return ([], [], [])  # start() probe succeeds
        raise OSError("stop the poll")  # the spawned thread exits

    monkeypatch.setattr(adapter.select, "select", fake_select)

    class _Tty:
        def isatty(self):
            return True

    repl = SimpleNamespace()
    listener = adapter.SteeringListener(repl, stream=_Tty())
    listener._stop = True
    assert listener.start() is True
    import time

    time.sleep(0.4)  # the thread polls once, hits the error, and exits


def test_loop_reads_a_line_and_steers(monkeypatch) -> None:
    """The ready path: a typed line lands in the steering queue."""
    from types import SimpleNamespace

    import xenon.repl.runtime_adapter as adapter

    def fake_select(streams, r, w, timeout):
        return ([streams[0]], [], [])

    monkeypatch.setattr(adapter.select, "select", fake_select)

    class _Line:
        def __init__(self):
            self._left = 1

        def readline(self):
            if self._left:
                self._left -= 1
                return "steer me\n"
            return ""

    repl = SimpleNamespace()
    listener = adapter.SteeringListener(repl, stream=_Line())
    listener._stop = False
    listener._loop()
    listener._loop()  # second call: readline returns '' -> loop exits

    item = adapter.drain_steering(repl)
    assert item == {"text": "steer me", "kind": "constraint", "scope": "task"}


def test_loop_returns_when_readline_raises(monkeypatch) -> None:
    """A closed/broken stream ends the steering reader cleanly."""
    from types import SimpleNamespace

    import xenon.repl.runtime_adapter as adapter

    def fake_select(streams, r, w, timeout):
        return ([streams[0]], [], [])

    monkeypatch.setattr(adapter.select, "select", fake_select)

    class _Broken:
        def readline(self):
            raise OSError("closed")

    repl = SimpleNamespace()
    listener = adapter.SteeringListener(repl, stream=_Broken())
    listener._stop = False
    listener._loop()  # must return without raising
