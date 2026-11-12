"""Cache-prefix guard rail (#372 D1).

A prefix cache only pays off if the bytes every request starts with are
byte-for-byte stable.  The kernel view is deterministic, but nothing locked it
down: a view edit (a new field, a re-sort, an injected note) would silently
floor the cache hit rate while every functional test stayed green.

These tests assert both directions:

1. the same spec + state builds the same prefix digest (and whole hash);
2. a change to ANY prefix field changes the digest (negative);
3. working-set growth does NOT change the prefix digest (the point of
   prefix caching) — even though the whole-hash changes.
"""

from __future__ import annotations

from tests.kernel.test_north_star import FakeCatalog
from xenon.kernel.contracts import TaskSpec
from xenon.kernel.view import build_model_view
from xenon.ledger.store import Ledger


def _spec(**kw) -> TaskSpec:
    base = dict(task_id="t", objective="o", workspace=".")
    base.update(kw)
    return TaskSpec(**base)


def _view(spec: TaskSpec, ledger: Ledger):
    return build_model_view(
        spec=spec, catalog=FakeCatalog(), ledger=ledger
    )


def test_identical_inputs_build_identical_prefix_digests() -> None:
    ledger = Ledger.in_memory("same")
    spec = _spec()

    first = _view(spec, ledger)
    second = _view(spec, ledger)

    assert first.prefix_digest
    assert first.prefix_digest == second.prefix_digest
    assert first.hash == second.hash


def test_working_set_growth_keeps_the_prefix_digest_stable() -> None:
    ledger = Ledger.in_memory("growth")
    spec = _spec()

    early = _view(spec, ledger)
    # Advance the ledger: a new event changes the working set (and the whole
    # hash) but must not touch the prefix.
    from xenon.ledger.events import EventKind, EventSource

    ledger.append(EventKind.THOUGHT, EventSource.LLM, {"text": "x"})
    late = _view(spec, ledger)

    assert late.hash != early.hash  # the model sees the new working set
    assert late.prefix_digest == early.prefix_digest  # the cache prefix holds


def test_a_constraint_change_changes_the_prefix_digest() -> None:
    ledger = Ledger.in_memory("constraint")
    plain = _view(_spec(), ledger)
    constrained = _view(_spec(constraints=("must use edit",)), ledger)

    assert constrained.prefix_digest != plain.prefix_digest
    assert constrained.hash != plain.hash


def test_a_note_does_not_touch_the_prefix_digest() -> None:
    """A note changes every turn, so it must never enter the cached prefix.

    It rides in this turn's message instead.  When it lived in the system
    prompt, the very first loop diagnosis invalidated the capability catalog
    and both action protocols for the rest of the run.
    """

    from xenon.harness.llm import build_messages

    ledger = Ledger.in_memory("note")
    plain = _view(_spec(), ledger)
    noted = build_model_view(
        spec=_spec(),
        catalog=FakeCatalog(),
        ledger=ledger,
        notes=("Loop detected: read failed 4 times. Change the approach.",),
    )

    assert noted.prefix_digest == plain.prefix_digest
    assert build_messages(noted)[0] == build_messages(plain)[0]
    assert "Loop detected" in build_messages(noted)[1]["content"]


def test_a_tool_index_change_changes_the_prefix_digest() -> None:
    ledger = Ledger.in_memory("tools")
    spec = _spec()
    empty = build_model_view(
        spec=spec, catalog=FakeCatalog(), ledger=ledger, phase=__import__(
            "xenon.kernel.contracts", fromlist=["TaskPhase"]
        ).TaskPhase.RUNNING
    )
    # A different catalog (no tools) is a different prefix.
    class NoTools:
        def specs(self):
            return ()

    bare = build_model_view(spec=spec, catalog=NoTools(), ledger=ledger)

    assert bare.prefix_digest != empty.prefix_digest


def test_turn_path_renders_only_when_non_empty() -> None:
    from xenon.harness.llm import build_messages
    from xenon.kernel.contracts import TaskPhase

    ledger = Ledger.in_memory("branch")
    plain = _view(_spec(), ledger)
    assert "Turn path" not in plain.system

    branched = build_model_view(
        spec=_spec(),
        catalog=FakeCatalog(),
        ledger=ledger,
        turn_path=("turn-1: first", "turn-2: second"),
        phase=TaskPhase.RUNNING,
    )
    # The branch path is fixed for the task, so it rides in the turn message
    # and never disturbs the prefix.
    assert branched.prefix_digest == plain.prefix_digest
    turn = build_messages(branched)[1]["content"]
    assert "Turn path (you are on a side branch): turn-1: first -> turn-2: second" in turn
    assert branched.turn_path == ("turn-1: first", "turn-2: second")


def test_status_snapshot_comes_from_runtime_facts() -> None:
    """#372 E1: the snapshot is the runtime's own counters, not guesses."""
    from tests.kernel.test_north_star import ScriptedStrategy, _runtime
    from xenon.kernel.contracts import CallTool, Capability, TaskSpec, ToolIntent

    ledger = Ledger.in_memory("snap")
    runtime = _runtime(ledger)
    runtime.run(
        TaskSpec(task_id="snap", objective="o", workspace="."),
        ScriptedStrategy([CallTool(ToolIntent(capability=Capability.READ))]),
    )

    snap = runtime.status_snapshot()
    assert snap["steps"] >= 1
    assert snap["phase"] in {"running", "blocked", "aborted", "delivered"}
    assert snap["stalled_seconds"] >= 0
    assert snap["tool_calls"] >= 1
