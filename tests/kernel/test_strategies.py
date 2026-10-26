"""Strategy seam tests: propose only, never execute (#198)."""

from __future__ import annotations

from types import SimpleNamespace

from xenon.kernel.contracts import (
    Budget,
    Claim,
    ClaimSource,
    Debt,
    Finish,
    ModelView,
    Obligation,
    PlanProposal,
    PropositionKind,
    TaskSpec,
    Think,
)
from xenon.kernel.strategies import (
    ActFirst,
    NoToolStrategy,
    PlanFirst,
    RepairFirst,
)


def _view(*, debt: tuple[Debt, ...] = ()) -> ModelView:
    spec = TaskSpec(task_id="t1", objective="x", workspace=".")
    return ModelView(
        task=spec,
        system="",
        tools=(),
        working_set=(),
        debt=debt,
        budget=Budget(),
        input_seq=0,
        hash="h",
        active_step=None,
        steps=(),
    )


def test_act_first_delegates() -> None:
    strategy = ActFirst(lambda view: Think("next"))
    assert isinstance(strategy.propose(_view()), Think)


def test_no_tool_strategy_returns_finish() -> None:
    claim = Claim(
        id="c1",
        kind=PropositionKind.ANSWER_DELIVERED,
        source=ClaimSource.LLM,
    )
    strategy = NoToolStrategy("hello", claims=(claim,))
    action = strategy.propose(_view())
    assert isinstance(action, Finish)
    assert action.answer == "hello"
    assert action.claims == (claim,)


def test_repair_first_prefers_repair_while_debt_is_open() -> None:
    debt = Debt(
        obligation=Obligation(
            id="o1", kind=PropositionKind.FILE_CHANGED, target="a.py"
        ),
        reason="missing",
    )
    fallback_called = False

    def fallback(view: ModelView):
        nonlocal fallback_called
        fallback_called = True
        return Think("fallback")

    def repair(view: ModelView, debts: tuple[Debt, ...]):
        assert debts
        return Think("repair")

    strategy = RepairFirst(
        fallback=SimpleNamespace(propose=fallback), repairer=repair
    )
    action = strategy.propose(_view(debt=(debt,)))
    assert isinstance(action, Think)
    assert action.text == "repair"
    assert fallback_called is False

    strategy.propose(_view())
    assert fallback_called is True


def test_plan_first_submits_once() -> None:
    strategy = PlanFirst(lambda view: PlanProposal(steps=()))
    first = strategy.propose(_view())
    assert hasattr(first, "plan")
    # An empty plan closes immediately; the strategy must not resubmit.
    second = strategy.propose(_view())
    assert isinstance(second, Finish)
