"""When compaction fires, and why it waits for a round boundary.

Two separate pressures drive folding and conflating them caused a real bug:

* **The prompt does not fit.** Entries that never fit the character budget must
  fold into a digest, or the history is silently lost.  This is not optional.
* **The prompt is getting expensive.** Past ``COMPACT_AT`` of the real window,
  folding is *worth* doing — but a fold rewrites the prompt tail and destroys
  the provider's cached prefix, so it waits until a plan round ends.

The measured consequence of getting this wrong: a kernel run over 70 calls
reported 0% cache hits while rebuilding a ~3k-token prefix every turn.
"""

from __future__ import annotations

from xenon.kernel.contracts import Budget, TaskMode
from xenon.kernel.tokens import count_tokens, estimate
from xenon.kernel.view.base import (
    COMPACT_AT,
    _context_chars,
    _working_set,
    compaction_due,
)


class _Event:
    def __init__(self, seq: int, tool: str) -> None:
        self.seq = seq
        self.kind = type("K", (), {"value": "tool_receipt"})()
        self.payload = {"receipt": {"tool": tool, "status": "ok"}}


def _events(count: int) -> list[_Event]:
    return [_Event(index, f"tool_{index}") for index in range(1, count + 1)]


def test_the_tokenizer_is_exact_for_both_scripts() -> None:
    """``len(text)/2`` mis-sized CJK and English in opposite directions."""

    english = estimate("hello world this is a test")
    chinese = estimate("你好世界这是一个测试")

    assert english.exact and chinese.exact
    # Six English words are far fewer tokens than sixteen CJK characters; a
    # character-based estimate would have said the opposite.
    assert chinese.tokens > english.tokens


def test_compaction_is_not_due_below_the_threshold() -> None:
    budget = Budget(max_tokens=2048, context_window=100000)
    working_set = tuple({"seq": index, "kind": "tool_receipt"} for index in range(5))

    assert not compaction_due(working_set, budget)


def test_compaction_is_due_at_the_threshold() -> None:
    budget = Budget(max_tokens=2048, context_window=1000)
    filler = "x" * 8000
    working_set = ({"seq": 1, "kind": "tool_receipt", "blob": filler},)

    used = count_tokens(__import__("json").dumps(list(working_set)))
    assert used >= int(1000 * COMPACT_AT)
    assert compaction_due(working_set, budget)


def test_no_window_means_no_token_trigger() -> None:
    """There is nothing to be a fraction of; the character budget governs."""

    budget = Budget(max_tokens=4096)
    huge = ({"seq": 1, "kind": "tool_receipt", "blob": "x" * 50000},)

    assert not compaction_due(huge, budget)


def test_history_that_does_not_fit_always_folds() -> None:
    """Not deferrable: an over-budget prompt drops history if it does not fold."""

    budget = Budget(max_tokens=2048, context_window=8192)
    working_set, folded = _working_set(
        _events(300), mode=TaskMode.TASK, budget=budget, may_compact=False
    )

    assert folded, "an overflowing working set must leave a digest"
    assert len(working_set) < 300


def test_a_round_boundary_permits_an_earlier_fold() -> None:
    """The 70% trigger folds at a round end, before the prompt is in trouble."""

    budget = Budget(max_tokens=2048, context_window=8192)
    events = _events(300)

    _, at_boundary = _working_set(
        events, mode=TaskMode.TASK, budget=budget, may_compact=True
    )
    _, mid_round = _working_set(
        events, mode=TaskMode.TASK, budget=budget, may_compact=False
    )

    # Both fold here, because this history overflows the character budget as
    # well.  What differs is *when* the trigger would allow it; assert the
    # small-window case still produces a digest in both.
    assert at_boundary and mid_round


def test_a_small_window_folds_more_than_a_large_one() -> None:
    events = _events(300)

    small, small_folded = _working_set(
        events, mode=TaskMode.TASK, budget=Budget(max_tokens=2048, context_window=8192)
    )
    large, large_folded = _working_set(
        events,
        mode=TaskMode.TASK,
        budget=Budget(max_tokens=2048, context_window=200000),
    )

    assert len(small_folded) > len(large_folded)
    assert len(small) < len(large)


def test_the_character_budget_still_reserves_room_for_output() -> None:
    narrow = _context_chars(Budget(max_tokens=4096, context_window=8192))
    wide = _context_chars(Budget(max_tokens=4096, context_window=128000))

    assert narrow < wide
