"""Decision-chain extractor: the 'why' summary survives compaction (#289).

Compaction must not flatten the reasons a task ended up the way it did.
plan_accepted / control_decision (incl. steering) / debt_opened carry those
reasons; when they are folded, their ``why`` lines stay on the digest entry.
"""

from __future__ import annotations

from xenon.kernel.compaction import fold_dropped
from xenon.kernel.decision_chain import decision_lines


def _summary(seq: int, event_kind: str, **payload) -> dict:
    """Mirror the real ``_summary`` field split: payload kinds never overwrite
    the event kind."""
    item = {"seq": seq, "kind": event_kind}
    if event_kind in {"control_decision"} and "kind" in payload:
        item["decision"] = payload.pop("kind")
    if event_kind in {"debt_opened", "debt_closed"} and "kind" in payload:
        item["debt_kind"] = payload.pop("kind")
    item.update(payload)
    return item


def _size(entry: dict) -> int:
    import json

    return len(json.dumps(entry, ensure_ascii=False))


# ── the extractor ─────────────────────────────────────────────────


def test_plan_acceptance_is_a_why_line() -> None:
    lines = decision_lines(
        [_summary(3, "plan_accepted", plan={"steps": [{"id": 1}, {"id": 2}]})]
    )

    assert lines == ["plan_accepted: 2 step(s)"]


def test_control_decisions_carry_their_reason() -> None:
    lines = decision_lines(
        [
            _summary(4, "control_decision", kind="deliver", reason="all claims verified"),
            _summary(9, "control_decision", kind="steering", reason="operator retracted"),
        ]
    )

    assert "control deliver: all claims verified" in lines
    assert "control steering: operator retracted" in lines


def test_debt_is_a_why_line() -> None:
    lines = decision_lines(
        [_summary(5, "debt_opened", kind="requirement_met", target="tests/test_x.py",
                  reason="no successful tool receipt")],
    )

    assert lines == [
        "debt requirement_met tests/test_x.py: no successful tool receipt"
    ]


def test_non_decision_events_produce_no_why() -> None:
    lines = decision_lines(
        [
            _summary(1, "tool_receipt", tool="bash", status="ok"),
            _summary(2, "thought", text="x"),
        ]
    )

    assert lines == []


def test_control_reasons_are_clipped() -> None:
    lines = decision_lines(
        [_summary(4, "control_decision", kind="deliver", reason="x" * 500)]
    )

    assert len(lines[0]) < 260


# ── the compaction integration ────────────────────────────────────


def test_folded_decisions_keep_their_why_lines() -> None:
    dropped = [
        _summary(1, "tool_receipt", tool="bash", status="ok"),
        _summary(2, "plan_accepted", plan={"steps": [{"id": 1}]}),
        _summary(3, "debt_opened", kind="requirement_met", target="x",
                 reason="no receipt"),
    ]
    entry = fold_dropped(dropped, max_chars=4000, used=0, size_of=_size)

    assert entry is not None
    assert entry["why"] == [
        "plan_accepted: 1 step(s)",
        "debt requirement_met x: no receipt",
    ]


def test_why_lines_yield_when_the_budget_is_tight() -> None:
    dropped = [
        _summary(1, "control_decision", kind="deliver", reason="y" * 300),
        _summary(2, "debt_opened", kind="requirement_met", target="x",
                 reason="z" * 300),
    ]
    entry = fold_dropped(dropped, max_chars=300, used=0, size_of=_size)

    # The digest still keeps at least one why line (or the fact with a why),
    # never silently dropping the reason entirely.
    assert entry is not None
    assert "why" in entry
    assert len(entry["why"]) == 1  # one line fits; the second yields


def test_why_lines_preserve_ledger_order() -> None:
    dropped = [
        _summary(1, "control_decision", kind="steering", reason="a"),
        _summary(2, "debt_opened", kind="requirement_met", target="x",
                 reason="b"),
        _summary(3, "plan_accepted", plan={"steps": []}),
    ]
    lines = decision_lines(dropped)

    assert lines == [
        "control steering: a",
        "debt requirement_met x: b",
        "plan_accepted: 0 step(s)",
    ]


def test_why_lines_yield_entirely_under_an_extreme_budget() -> None:
    """room < minimal-entry + one why line: every why yields, but the fact
    (seqs) survives — nothing is silently dropped."""
    dropped = [
        _summary(1, "control_decision", decision="deliver", reason="y" * 300),
    ]
    entry = fold_dropped(dropped, max_chars=100, used=0, size_of=_size)

    assert entry is not None
    assert "why" not in entry
    assert entry["seqs"] == [1]
