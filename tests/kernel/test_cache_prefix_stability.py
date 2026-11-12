"""A cached prefix ends at the first differing byte.

Every line placed ahead of a stable block is paid for on every call once any
earlier line changes.  A real kernel run measured **0% cache hits over 70
calls**: `Phase:` sat fourth in the system prompt, ahead of the capability
catalog and the action protocols, so each turn's phase flip invalidated
everything after it.

These tests pin the ordering invariant rather than a token count, because the
ordering is what the cache depends on and the counts move with every prompt
edit.
"""

from __future__ import annotations

from xenon.catalog.specs import CORE_SPECS
from xenon.harness.llm import build_messages, render_state
from xenon.kernel.contracts import (
    Debt,
    Obligation,
    PropositionKind,
    TaskPhase,
    TaskSpec,
)
from xenon.kernel.view import build_model_view
from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.store import Ledger


class _Catalog:
    def specs(self, profile: str = "core"):
        return CORE_SPECS


def _ledger() -> Ledger:
    ledger = Ledger.in_memory("t")
    ledger.append(
        EventKind.TASK_STARTED,
        EventSource.RUNTIME,
        {"task_id": "t", "objective": "fix it", "workspace": "/ws"},
    )
    ledger.append(
        EventKind.WORKSPACE_LAYOUT,
        EventSource.RUNTIME,
        {"rendered": "Workspace layout: .: src/"},
    )
    return ledger


def _view(phase: TaskPhase, *, notes: tuple[str, ...] = (), debt: tuple[Debt, ...] = ()):
    spec = TaskSpec(task_id="t", objective="fix it", workspace="/ws")
    return build_model_view(
        spec=spec,
        catalog=_Catalog(),
        ledger=_ledger(),
        phase=phase,
        notes=notes,
        debt=debt,
    )


def _common_prefix(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    for index in range(limit):
        if left[index] != right[index]:
            return index
    return limit


def test_a_phase_change_does_not_invalidate_the_catalog() -> None:
    """The exact defect that produced 0% hits: phase sat ahead of the catalog."""

    running = _view(TaskPhase.RUNNING).system
    thinking = _view(TaskPhase.THINKING).system

    shared = _common_prefix(running, thinking)
    catalog_at = running.index("Capability catalog")

    assert shared > catalog_at, (
        "a phase change must not truncate the cached prefix before the "
        f"capability catalog (shared={shared}, catalog at {catalog_at})"
    )


def test_notes_and_debt_appear_after_the_catalog() -> None:
    debt = (
        Debt(
            obligation=Obligation(
                id="o1", kind=PropositionKind.FILE_CHANGED, target="a.py"
            ),
            reason="no fact",
        ),
    )
    plain = _view(TaskPhase.RUNNING)
    noisy = _view(TaskPhase.RUNNING, notes=("retry",), debt=debt)

    shared = _common_prefix(plain.system, noisy.system)

    assert shared > plain.system.index("Capability catalog")


def test_the_stable_head_is_shared_across_a_whole_task() -> None:
    """Two views of the same task share most of the system prompt."""

    first = _view(TaskPhase.RUNNING)
    later = _view(TaskPhase.THINKING, notes=("n",))

    shared = _common_prefix(first.system, later.system)

    assert shared / len(first.system) > 0.85


def test_the_prefix_is_byte_identical_when_only_the_turn_changes() -> None:
    """The whole point: turn N+1 must not move a single byte of the prefix."""

    first = _view(TaskPhase.RUNNING)
    later = _view(TaskPhase.THINKING, notes=("n",), debt=(
        Debt(
            obligation=Obligation(
                id="o1", kind=PropositionKind.FILE_CHANGED, target="a.py"
            ),
            reason="no fact",
        ),
    ))

    assert build_messages(first)[0] == build_messages(later)[0]


def test_state_is_not_shipped_it_is_asked_for() -> None:
    """Phase, budget and debt never appear in a request.

    They change every turn, so shipping them would rewrite the bytes where
    the previous copy ended and truncate the cached prefix there.  The model
    reads them through ``get_state`` instead.
    """

    view = _view(TaskPhase.RUNNING)
    messages = build_messages(view)

    assert len(messages) == 2
    body = messages[0]["content"] + messages[1]["content"]
    assert '"phase"' not in body
    assert '"debt"' not in body

    state = render_state(view, "state", {})
    assert '"phase":"running"' in state
    assert '"debt":[]' in state


def test_each_request_is_the_prefix_plus_one_turn() -> None:
    """No history is replayed, so there is nothing to grow and nothing to break.

    The previous shape replayed one message per event: correct for caching but
    the history still grew without bound, and the per-turn state message that
    followed it was rewritten each round.  Both are gone.
    """

    from xenon.kernel.contracts import ModelView

    plain = _view(TaskPhase.RUNNING)
    grown = ModelView(
        task=plain.task,
        system=plain.system,
        tools=plain.tools,
        working_set=(
            {"seq": 1, "kind": "thought", "text": "look"},
            {"seq": 2, "kind": "tool_receipt", "tool": "read", "status": "ok"},
        ),
        debt=(),
        budget=plain.budget,
        input_seq=2,
        hash="h2",
        phase=TaskPhase.RUNNING,
    )

    base = build_messages(plain)
    later = build_messages(grown)

    assert base[0] == later[0]
    assert len(base) == len(later) == 2
    # Only the newest event travels; earlier ones are reachable via get_state.
    assert '"seq":2' in later[1]["content"]
    assert '"seq":1' not in later[1]["content"]
