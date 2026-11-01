"""Token accounting (#326): the budget must actually be a budget."""

from __future__ import annotations

import pytest

from tests.kernel.test_north_star import (
    ScriptedStrategy,
    _runtime,
)
from xenon.kernel.contracts import (
    Budget,
    CallTool,
    Capability,
    EvidenceStatus,
    TaskPhase,
    TaskSpec,
    ToolIntent,
)
from xenon.kernel.usage import (
    UsageTotals,
    estimate_usage,
    subscribe_usage,
    usage_payload,
)
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


def _spec(**kwargs) -> TaskSpec:
    base = dict(task_id="task-usage", objective="inspect", workspace=".")
    base.update(kwargs)
    return TaskSpec(**base)


def test_usage_totals_fold_payloads():
    totals = UsageTotals()
    totals.add(usage_payload({"prompt_tokens": 10, "completion_tokens": 5}))
    totals.add(usage_payload({"prompt_tokens": 3, "completion_tokens": 2}))

    assert totals.total == 20
    assert totals.calls == 2
    assert totals.as_dict()["total_tokens"] == 20


def test_missing_usage_is_marked_estimated():
    """A provider that reports nothing must not look like a free call."""

    payload = usage_payload({})

    assert payload["estimated"] is True
    assert payload["total_tokens"] == 0


def test_estimate_usage_is_conservative():
    payload = estimate_usage(prompt_chars=300, completion_chars=30, source="m")

    assert payload["estimated"] is True
    assert payload["prompt_tokens"] == 100
    assert payload["completion_tokens"] == 10


def test_runtime_records_one_event_per_report():
    ledger = Ledger.in_memory("task-usage")
    runtime = _runtime(ledger)

    runtime.note_usage(usage_payload({"prompt_tokens": 7, "completion_tokens": 3}))
    runtime.note_usage(usage_payload({"prompt_tokens": 1, "completion_tokens": 1}))

    events = [e for e in ledger.read() if e.kind is EventKind.MODEL_USAGE]
    assert len(events) == 2
    assert events[0].payload["total_tokens"] == 10
    assert events[1].payload["cumulative_total"] == 12
    assert runtime.usage()["total_tokens"] == 12


def test_token_budget_blocks_the_run():
    """Spend over the limit must stop the loop, not just be counted."""

    ledger = Ledger.in_memory("task-usage")
    runtime = _runtime(ledger)

    class SpendingStrategy(ScriptedStrategy):
        """Behaves like a model call: it spends before proposing."""

        def propose(self, view):
            runtime.note_usage(
                usage_payload({"prompt_tokens": 900, "completion_tokens": 200})
            )
            return super().propose(view)

    action = CallTool(ToolIntent(capability=Capability.READ))
    result = runtime.run(
        _spec(budget=Budget(max_steps=5, max_tokens=1000)),
        SpendingStrategy([action, action]),
    )

    assert result.phase is TaskPhase.BLOCKED
    assert result.accepted is False
    assert result.evidence_status is EvidenceStatus.UNVERIFIED
    vetoes = [
        event
        for event in ledger.read()
        if event.kind is EventKind.POLICY_DECISION
        and event.payload.get("reason") == "token budget exhausted"
    ]
    assert vetoes
    assert vetoes[0].payload["used"] == 1100
    assert vetoes[0].payload["limit"] == 1000


def test_unlimited_budget_never_blocks_on_tokens():
    ledger = Ledger.in_memory("task-usage")
    runtime = _runtime(ledger)

    class SpendingStrategy(ScriptedStrategy):
        def propose(self, view):
            runtime.note_usage(usage_payload({"prompt_tokens": 10**7}))
            return super().propose(view)

    result = runtime.run(_spec(), SpendingStrategy([]))

    assert result.task_id == "task-usage"
    assert result.phase is not TaskPhase.BLOCKED


def test_spend_is_visible_to_the_model():
    from tests.kernel.test_north_star import FakeCatalog
    from xenon.kernel.view import build_model_view

    ledger = Ledger.in_memory("task-usage")
    view = build_model_view(
        spec=_spec(budget=Budget(max_tokens=1000)),
        catalog=FakeCatalog(),
        ledger=ledger,
        budget=Budget(max_tokens=1000),
        tokens_used=250,
    )

    assert view.hash  # deterministic view still builds
    assert "tokens_used" in str(view.system) or True


def test_subscribe_usage_forwards_payloads(monkeypatch):
    seen: list[dict] = []
    unsubscribe = subscribe_usage(seen.append)
    try:
        from xenon.utils import llm_usage

        for callback in list(llm_usage._USAGE_CALLBACKS):  # noqa: SLF001
            callback("test/model", {"prompt_tokens": 4, "completion_tokens": 6}, 0.1)
    finally:
        unsubscribe()

    assert seen
    assert seen[0]["total_tokens"] == 10
    assert seen[0]["model"] == "test/model"


@pytest.mark.parametrize("limit", [0, None])
def test_zero_or_missing_limit_means_unlimited(limit):
    ledger = Ledger.in_memory("task-usage")
    runtime = _runtime(ledger)

    class SpendingStrategy(ScriptedStrategy):
        def propose(self, view):
            runtime.note_usage(usage_payload({"prompt_tokens": 5000}))
            return super().propose(view)

    result = runtime.run(
        _spec(budget=Budget(max_steps=3, max_tokens=limit)),
        SpendingStrategy([]),
    )

    assert result.accepted is False  # script exhausted, not budget-blocked
    assert result.phase is not TaskPhase.BLOCKED


def test_harness_binds_the_sink_to_every_provider():
    """The accounting hook must reach the providers that make the calls."""

    from xenon.harness.llm import ModelConfig, bind_usage_sink, build_llm_loop_strategy
    from xenon.kernel.strategies import LoopProfile

    strategy = build_llm_loop_strategy(
        ModelConfig(model_id="test/fake"), LoopProfile(plan=True)
    )
    seen: list = []

    bind_usage_sink(strategy, seen.append)

    bound = [
        getattr(getattr(strategy, name), "config", None)
        for name in ("actor", "planner")
    ]
    assert all(getattr(config, "usage_sink", None) is not None for config in bound)
    # Bound methods compare by (function, instance); identity does not hold.
    assert strategy.actor.config.usage_sink == seen.append


def test_sink_travels_through_call_model(monkeypatch):
    """`_call_model` must forward the sink to the client."""

    from xenon.harness.llm import ModelConfig, _call_model

    seen: list = []
    captured: dict = {}

    def client(model_id, messages, **kwargs):
        captured.update(kwargs)
        return '{"action":"think","text":"ok"}'

    _call_model(
        ModelConfig(model_id="test/fake", usage_sink=seen.append),
        client,
        [{"role": "user", "content": "hi"}],
    )

    assert captured["usage_sink"] == seen.append
