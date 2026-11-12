"""Dual-summary fold (design doc §13 decision 1 / defect 4).

The fold has two halves, both appended at the tail so the ``system + tools``
cache prefix is untouched:

* ``CONTEXT_COMPACTED`` — deterministic marker, always written at a fold;
* ``CONTEXT_SUMMARY`` — the model-generated continuation narrative, written
  only when a SummaryProvider returns one.

The deterministic digest stays the fact backbone; a provider that raises,
returns "" or is absent degrades to a deterministic-only fold — never a failed
task.  These tests pin that contract and the once-per-fold guard.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from tests.kernel.test_north_star import _runtime
from xenon.harness.llm import ModelConfig
from xenon.harness.summarizer import LLMSummarizer, folded_lines
from xenon.kernel.contracts import Budget, TaskSpec
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


class _FakeProvider:
    def __init__(self, narrative: Any = "EARLIER: fixed add() in shapes.py") -> None:
        self.narrative = narrative
        self.calls: list[dict[str, Any]] = []
        self.config = SimpleNamespace(model_id="m/test")

    def summarize(self, *, objective: str, seqs, folded) -> str:
        self.calls.append(
            {"objective": objective, "seqs": list(seqs), "folded": list(folded)}
        )
        if isinstance(self.narrative, Exception):
            raise self.narrative
        return self.narrative


def _view(compacted, folded_items=None):
    if folded_items is None:
        folded_items = tuple(
            {"seq": seq, "kind": "tool_receipt", "tool": "t"} for seq in compacted
        )
    return SimpleNamespace(
        compacted=tuple(compacted),
        folded_items=tuple(folded_items),
        input_seq=10,
    )


def _spec() -> TaskSpec:
    return TaskSpec(
        task_id="t1",
        objective="fix shapes",
        workspace=".",
        budget=Budget(max_steps=10),
    )


def _kinds(ledger: Ledger) -> list[str]:
    return [event.kind for event in ledger.read()]


def _runtime_with(provider: Any) -> Any:
    ledger = Ledger.in_memory("t1")
    runtime = _runtime(ledger)
    runtime._summary_provider = provider
    return runtime, ledger


# ── runtime fold recording ───────────────────────────────────────────


def test_a_fold_with_a_provider_records_marker_and_summary() -> None:
    provider = _FakeProvider()
    runtime, ledger = _runtime_with(provider)
    items = ({"seq": 3, "kind": "tool_receipt", "tool": "read"},)

    runtime._record_fold(_view((3,), items), _spec())

    assert EventKind.CONTEXT_COMPACTED in _kinds(ledger)
    assert EventKind.CONTEXT_SUMMARY in _kinds(ledger)
    # The provider saw the objective, the folded seqs and the folded items.
    assert provider.calls[0]["seqs"] == [3]
    assert provider.calls[0]["folded"] == list(items)
    assert provider.calls[0]["objective"] == "fix shapes"
    # One call only — the rebuilt view cannot trigger a second summary.
    assert len(provider.calls) == 1
    # The narrative is durable in the ledger; it rides in the working set
    # whenever the rebuilt view still folds (in production the fold only
    # happens because the prompt is over budget, so it does).
    summaries = [
        event.payload
        for event in ledger.read()
        if event.kind is EventKind.CONTEXT_SUMMARY
    ]
    assert summaries and "fixed add()" in summaries[0]["summary"]


def test_empty_narrative_records_only_the_marker() -> None:
    provider = _FakeProvider(narrative="")
    runtime, ledger = _runtime_with(provider)

    runtime._record_fold(_view((3,)), _spec())

    assert EventKind.CONTEXT_COMPACTED in _kinds(ledger)
    assert EventKind.CONTEXT_SUMMARY not in _kinds(ledger)
    assert len(provider.calls) == 1


def test_a_provider_that_raises_degrades_to_deterministic_fold() -> None:
    provider = _FakeProvider(narrative=RuntimeError("network down"))
    runtime, ledger = _runtime_with(provider)

    runtime._record_fold(_view((3,)), _spec())  # must not raise

    assert EventKind.CONTEXT_COMPACTED in _kinds(ledger)
    assert EventKind.CONTEXT_SUMMARY not in _kinds(ledger)


def test_no_provider_records_only_the_marker() -> None:
    ledger = Ledger.in_memory("t1")
    runtime = _runtime(ledger)
    assert runtime._summary_provider is None

    runtime._record_fold(_view((3,)), _spec())

    assert EventKind.CONTEXT_COMPACTED in _kinds(ledger)
    assert EventKind.CONTEXT_SUMMARY not in _kinds(ledger)


def test_the_same_fold_is_recorded_once() -> None:
    provider = _FakeProvider()
    runtime, ledger = _runtime_with(provider)

    runtime._record_fold(_view((3, 4)), _spec())
    runtime._record_fold(_view((3, 4)), _spec())

    assert _kinds(ledger).count(EventKind.CONTEXT_COMPACTED) == 1
    assert len(provider.calls) == 1


def test_no_compaction_means_no_events() -> None:
    runtime, ledger = _runtime_with(_FakeProvider())

    out = runtime._record_fold(_view(()), _spec())

    assert out.compacted == ()
    assert _kinds(ledger) == []


# ── pure helpers and the harness summariser ──────────────────────────


def test_folded_lines_renders_structural_fields() -> None:
    lines = folded_lines(
        [
            {
                "seq": 7,
                "kind": "tool_receipt",
                "tool": "bash",
                "status": "failed",
                "error": "exit 1",
            }
        ]
    )

    assert lines == ["seq=7 tool_receipt tool=bash status=failed | error=exit 1"]


def test_llm_summarizer_returns_the_client_text() -> None:
    called = {}

    def client(model_id, messages, **kwargs):
        called["model_id"] = model_id
        called["system"] = messages[0]["content"]
        return "CONTINUATION BRIEF"

    summarizer = LLMSummarizer(
        ModelConfig(model_id="m/test"),
        client=client,
    )

    text = summarizer.summarize(
        objective="o", seqs=[1, 2], folded=[{"seq": 1, "kind": "thought"}]
    )

    assert text == "CONTINUATION BRIEF"
    # Uses the turn model and the no-new-facts protocol.
    assert called["model_id"] == "m/test"
    assert "Do not infer" in called["system"]


def test_llm_summarizer_returns_empty_when_the_client_raises() -> None:
    def client(*args, **kwargs):
        raise RuntimeError("boom")

    summarizer = LLMSummarizer(
        ModelConfig(model_id="m/test"), client=client
    )

    assert (
        summarizer.summarize(
            objective="o", seqs=[1], folded=[{"seq": 1, "kind": "thought"}]
        )
        == ""
    )


def test_llm_summarizer_caps_its_output_budget() -> None:
    summarizer = LLMSummarizer(
        ModelConfig(model_id="m/test", max_tokens=8192)
    )

    assert summarizer.config.max_tokens == 1024


def test_the_fold_narrative_is_pushed_into_the_turn() -> None:
    """A generated narrative must reach the model's prompt, not sit in the
    pull-only working set.  Without this the model re-did the compacted work."""

    from xenon.harness.llm import current_turn

    view = SimpleNamespace(
        notes=(),
        turn_path=(),
        task=SimpleNamespace(objective="read files"),
        active_step=None,
        session_history=(),
        working_set=[
            {"seq": 1, "kind": "tool_receipt", "tool": "read", "status": "ok"},
            {"seq": 2, "kind": "context_summary", "summary_note": "BRIEF: mod_1 done, read mod_2"},
        ],
    )

    text = current_turn(view, instruction="Call one tool.")

    assert "continuation brief" in text
    assert "BRIEF: mod_1 done" in text
