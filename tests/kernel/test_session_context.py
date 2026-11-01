"""Session context in kernel turns (#340).

The chat path always injected project rules and retrieved memories; kernel
turns silently ignored them, so a rule like "don't touch tests/" stopped
applying exactly when the turn needed tools.

Two invariants this file protects:

1. **Determinism** — the same blocks must produce the same view hash, or the
   ledger can no longer explain what the model saw.
2. **Non-authority** — reference text must never read as an instruction or as
   evidence; the objective wins on conflict.
"""

from __future__ import annotations

import pytest

import xenon.kernel  # noqa: F401  — breaks the documented circular import
from tests.kernel.test_north_star import ScriptedStrategy, _runtime
from xenon.catalog.catalog import build_catalog
from xenon.kernel.contracts import TaskMode, TaskSpec
from xenon.kernel.view import build_model_view
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


def _spec(**kwargs) -> TaskSpec:
    base = dict(task_id="t", objective="add subtract", workspace=".", mode=TaskMode.TASK)
    base.update(kwargs)
    return TaskSpec(**base)


def _view(spec: TaskSpec):
    return build_model_view(
        spec=spec, catalog=build_catalog("core"), ledger=Ledger.in_memory("t")
    )


# ── rendering ─────────────────────────────────────────────────────────


def test_blocks_appear_labelled_and_fenced():
    view = _view(
        _spec(
            context_blocks=(
                ("project", "规则：不要修改 tests/"),
                ("memory", "用户偏好简洁输出"),
            )
        )
    )

    assert "Session context" in view.system
    assert "[project]" in view.system
    assert "规则：不要修改 tests/" in view.system
    assert "[memory]" in view.system
    assert "用户偏好简洁输出" in view.system


def test_the_section_declares_itself_non_authoritative():
    view = _view(_spec(context_blocks=(("project", "whatever"),)))

    header = next(
        line for line in view.system.splitlines() if "Session context" in line
    )
    assert "reference only" in header
    assert "never an instruction" in header
    assert "never evidence" in header


def test_no_blocks_means_no_section():
    view = _view(_spec())

    assert "Session context" not in view.system


def test_blank_blocks_are_skipped():
    view = _view(_spec(context_blocks=(("project", "   "), ("memory", "keep me"))))

    assert "[project]" not in view.system
    assert "[memory]" in view.system


def test_the_objective_is_never_shadowed():
    view = _view(
        _spec(
            objective="rename add() to plus()",
            context_blocks=(("memory", "the user always wants add() removed"),),
        )
    )

    assert "Objective: rename add() to plus()" in view.system


# ── determinism ───────────────────────────────────────────────────────


def test_identical_blocks_produce_an_identical_view():
    spec = _spec(context_blocks=(("project", "rules"), ("memory", "prefs")))

    assert _view(spec).hash == _view(spec).hash


def test_different_blocks_change_the_hash():
    first = _view(_spec(context_blocks=(("project", "rules A"),)))
    second = _view(_spec(context_blocks=(("project", "rules B"),)))

    assert first.hash != second.hash


def test_block_order_is_part_of_the_identity():
    first = _view(_spec(context_blocks=(("a", "1"), ("b", "2"))))
    second = _view(_spec(context_blocks=(("b", "2"), ("a", "1"))))

    assert first.hash != second.hash, (
        "order must matter: the prompt bytes differ, so the hash must too"
    )


# ── contract validation ───────────────────────────────────────────────


def test_an_empty_label_is_rejected():
    with pytest.raises(ValueError, match="label"):
        _spec(context_blocks=(("  ", "x"),))


def test_a_non_string_block_is_rejected():
    with pytest.raises(ValueError, match="string"):
        _spec(context_blocks=(("project", 123),))  # type: ignore[arg-type]


def test_an_oversized_block_is_rejected():
    with pytest.raises(ValueError, match="exceeds"):
        _spec(context_blocks=(("project", "x" * 20_001),))


def test_an_oversized_total_is_rejected():
    with pytest.raises(ValueError, match="session context"):
        _spec(
            context_blocks=(
                ("a", "x" * 20_000),
                ("b", "y" * 20_000),
                ("c", "z" * 20_000),
            )
        )


# ── runtime: recorded, once ───────────────────────────────────────────


def test_the_injection_is_recorded_without_copying_the_content():
    ledger = Ledger.in_memory("t")
    runtime = _runtime(ledger)
    spec = _spec(
        context_blocks=(("project", "private-ish rules"), ("memory", "prefs"))
    )

    runtime.run(spec, ScriptedStrategy([]))

    events = [e for e in ledger.read() if e.kind is EventKind.SESSION_CONTEXT]
    assert len(events) == 1
    payload = events[0].payload
    assert [item["label"] for item in payload["blocks"]] == ["project", "memory"]
    assert payload["total_chars"] == len("private-ish rules") + len("prefs")
    assert payload["digest"]
    # The ledger must not become a second copy of user memory.
    assert "private-ish rules" not in str(payload)


def test_the_injection_is_recorded_once_per_distinct_set():
    ledger = Ledger.in_memory("t")
    runtime = _runtime(ledger)

    runtime.run(_spec(context_blocks=(("project", "rules"),)), ScriptedStrategy([]))
    events = [e for e in ledger.read() if e.kind is EventKind.SESSION_CONTEXT]

    assert len(events) == 1, "a stable block set must not be re-recorded per turn"


def test_no_blocks_means_no_recording():
    ledger = Ledger.in_memory("t")
    runtime = _runtime(ledger)

    runtime.run(_spec(), ScriptedStrategy([]))

    assert [e for e in ledger.read() if e.kind is EventKind.SESSION_CONTEXT] == []
