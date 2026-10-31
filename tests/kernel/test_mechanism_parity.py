"""Mechanism parity for the single loop (#322).

The engine layer used to own three behaviours that live *inside* the loop,
not inside an engine: deterministic context compaction, a record that
compaction happened, and a repetition guard.  These tests pin them to the
kernel so the demolition cannot silently take them away.
"""

from __future__ import annotations

import pytest

from xenon.kernel.contracts import Budget, EvidenceStatus, TaskMode, TaskSpec
from xenon.kernel.guard import RepeatedCallGuard, call_signature
from xenon.kernel.runtime import LoopRuntime
from xenon.kernel.view.base import _working_set
from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.store import Ledger


def _fakes():
    from tests.kernel.test_north_star import FakeCatalog, FakeGateway  # noqa: PLC0415
    from xenon.kernel.controller import VerificationController  # noqa: PLC0415

    return {
        "catalog": FakeCatalog(),
        "gateway": FakeGateway(),
        "controller": VerificationController(),
    }


def _spec(**kwargs) -> TaskSpec:
    base = dict(
        task_id="t-compact",
        objective="inspect the repo",
        workspace=".",
        mode=TaskMode.TASK,
    )
    base.update(kwargs)
    return TaskSpec(**base)


def _receipt(seq: int, tool: str, status: str = "ok") -> dict:
    return {
        "seq": seq,
        "kind": "tool_receipt",
        "payload": {
            "receipt": {
                "tool": tool,
                "status": status,
                "receipt_id": f"rc_{seq}",
                "effects": [],
            },
            "fact_ids": [],
        },
    }


class _Event:
    """Minimal stand-in for a ledger event (seq/kind/value/payload)."""

    class kind:  # noqa: N801 - mirrors the ledger event shape
        value = "tool_receipt"

    def __init__(self, seq: int, tool: str, status: str = "ok") -> None:
        self.seq = seq
        self.kind = type("K", (), {"value": "tool_receipt"})()
        self.payload = {
            "receipt": {
                "tool": tool,
                "status": status,
                "receipt_id": f"rc_{seq}",
                "effects": [],
            },
            "fact_ids": [],
        }


def test_compaction_keeps_a_digest_instead_of_dropping_history():
    events = [_Event(i, f"tool_{i}", "ok" if i % 2 else "failed") for i in range(1, 60)]
    budget = Budget(max_tokens=600)

    working_set, compacted = _working_set(events, mode=TaskMode.TASK, budget=budget)

    assert compacted, "budget pressure must fold something"
    digest = [item for item in working_set if item.get("kind") == "elided"]
    assert len(digest) == 1
    entry = digest[0]
    # The invariant: folded events are still accounted for, never silently lost.
    assert entry["count"] == len(compacted)
    assert entry["seqs"] == list(compacted)
    assert entry["seqs"][-1] == compacted[-1]


def test_digest_keeps_status_and_seq_when_there_is_room():
    events = [_Event(i, "pytest", "failed" if i == 2 else "ok") for i in range(1, 14)]

    working_set, compacted = _working_set(
        events, mode=TaskMode.TASK, budget=Budget(max_tokens=560)
    )

    assert compacted
    entry = next(item for item in working_set if item.get("kind") == "elided")
    assert entry["lines"], "with room available the digest keeps one line per event"
    assert all(line.startswith("seq=") for line in entry["lines"])
    assert all("tool=pytest" in line for line in entry["lines"])
    # Failures are pinned by priority, so they never need the digest.
    failures = [
        item
        for item in working_set
        if item.get("kind") == "tool_receipt" and item.get("status") != "ok"
    ]
    assert failures and 2 not in compacted


def test_compaction_leaves_small_histories_untouched():
    events = [_Event(i, "read") for i in range(1, 3)]

    working_set, compacted = _working_set(
        events, mode=TaskMode.TASK, budget=Budget(max_tokens=8192)
    )

    assert compacted == ()
    assert all(item.get("kind") != "elided" for item in working_set)


def test_priority_events_survive_compaction():
    events = [_Event(i, f"tool_{i}") for i in range(1, 40)]
    events.append(
        type(
            "E",
            (),
            {
                "seq": 900,
                "kind": type("K", (), {"value": "debt_opened"})(),
                "payload": {"obligation_id": "o1", "reason": "no passing test"},
            },
        )()
    )

    working_set, _ = _working_set(
        events, mode=TaskMode.TASK, budget=Budget(max_tokens=600)
    )

    assert any(item.get("kind") == "debt_opened" for item in working_set)


def test_guard_trips_after_the_limit():
    guard = RepeatedCallGuard(max_identical=2)
    signature = call_signature("shell", {"command": "pytest -q"})

    assert guard.register(signature) is False
    assert guard.register(signature) is False
    assert guard.register(signature) is True
    assert guard.count(signature) == 3


def test_guard_distinguishes_arguments():
    guard = RepeatedCallGuard(max_identical=1)
    first = call_signature("shell", {"command": "pytest -q"})
    second = call_signature("shell", {"command": "pytest -q tests/kernel"})

    assert first != second
    assert guard.register(first) is False
    assert guard.register(second) is False
    assert guard.register(first) is True


def test_guard_rejects_a_nonsense_limit():
    with pytest.raises(ValueError):
        RepeatedCallGuard(max_identical=0)


def test_compaction_is_recorded_in_the_ledger(tmp_path):
    ledger = Ledger.in_memory("t-compact")
    runtime = LoopRuntime(ledger=ledger, **_fakes())

    view = type(
        "V",
        (),
        {"compacted": (3, 4, 5), "input_seq": 9},
    )()
    runtime._note_compaction(view)
    runtime._note_compaction(view)  # same fold, must not duplicate

    events = [e for e in ledger.read() if e.kind is EventKind.CONTEXT_COMPACTED]
    assert len(events) == 1
    assert events[0].source is EventSource.RUNTIME
    assert events[0].payload["seqs"] == [3, 4, 5]
    assert events[0].payload["count"] == 3


def test_compaction_absent_is_not_recorded(tmp_path):
    ledger = Ledger.in_memory("t-compact")
    runtime = LoopRuntime(ledger=ledger, **_fakes())

    runtime._note_compaction(type("V", (), {"compacted": (), "input_seq": 1})())

    assert [e for e in ledger.read() if e.kind is EventKind.CONTEXT_COMPACTED] == []


def test_repeated_tool_call_blocks_the_run():
    """The guard must stop a looping model and say why."""

    from tests.kernel.test_north_star import (  # noqa: PLC0415
        ScriptedStrategy,
        _runtime,
    )
    from xenon.kernel.contracts import CallTool, Capability, ToolIntent
    from xenon.ledger.store import Ledger

    ledger = Ledger.in_memory("task-repeat")
    runtime = _runtime(ledger)
    action = CallTool(ToolIntent(capability=Capability.READ))

    result = runtime.run(
        TaskSpec(task_id="task-repeat", objective="inspect", workspace="."),
        ScriptedStrategy([action, action, action, action, action]),
    )

    assert result.accepted is False
    assert result.evidence_status is EvidenceStatus.UNVERIFIED
    vetoes = [
        event
        for event in ledger.read()
        if event.kind is EventKind.POLICY_DECISION
        and event.payload.get("reason") == "repeated identical tool call"
    ]
    assert vetoes, "the veto must be in the ledger, not only in the result"
    # Three identical calls are allowed; the fourth is the one that trips it.
    receipts = [e for e in ledger.read() if e.kind is EventKind.TOOL_RECEIPT]
    assert len(receipts) == 3


# ── 上下文窗口感知（#322 / docs/design/mechanism-fit.md）──────────────


def test_context_window_sizes_the_working_set():
    """A small window must fold earlier than a large one, at the same output
    budget.  This is the whole point of knowing the real window."""

    events = [_Event(i, f"tool_{i}") for i in range(1, 300)]

    small, small_folded = _working_set(
        events,
        mode=TaskMode.TASK,
        budget=Budget(max_tokens=2048, context_window=8192),
    )
    large, large_folded = _working_set(
        events,
        mode=TaskMode.TASK,
        budget=Budget(max_tokens=2048, context_window=200000),
    )

    assert len(small_folded) > len(large_folded)
    assert len(small) < len(large)


def test_unknown_context_window_falls_back_to_the_heuristic():
    from xenon.kernel.view.base import _context_chars

    budget = Budget(max_tokens=4096)

    assert _context_chars(budget) == 8192


def test_context_window_reserves_room_for_output():
    from xenon.kernel.view.base import _context_chars

    narrow = _context_chars(Budget(max_tokens=4096, context_window=8192))
    wide = _context_chars(Budget(max_tokens=4096, context_window=128000))

    assert narrow < wide
    # The reserve keeps the history from claiming the entire window.
    assert narrow <= (8192 - 4096) * 2
