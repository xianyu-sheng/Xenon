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
from xenon.harness.llm import build_messages
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


def test_the_state_message_keeps_volatile_fields_last() -> None:
    """The working set is the bulk of the prompt; it must not sit behind phase."""

    view = _view(TaskPhase.RUNNING)
    payload = build_messages(view)[1]["content"]
    body = payload.split("Current deterministic state", 1)[1]

    assert body.index("available_tools") < body.index("recent_events")
    assert body.index("recent_events") < body.index('"phase"')
    assert body.index('"phase"') < body.index('"debt"')
