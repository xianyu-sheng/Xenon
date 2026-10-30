"""Strategy seam tests: propose only, never execute (#198)."""

from __future__ import annotations

from types import SimpleNamespace

from xenon.kernel.contracts import (
    Budget,
    CallTool,
    Capability,
    Claim,
    ClaimSource,
    Debt,
    Finish,
    ModelView,
    Obligation,
    PlanProposal,
    PropositionKind,
    StepContract,
    StepKind,
    SubmitPlan,
    TaskSpec,
    Think,
    ToolIntent,
)
from xenon.kernel.strategies import (
    ActFirst,
    NoToolStrategy,
    PlanFirst,
    ReflectFirst,
    RepairFirst,
)


def _view(
    *,
    debt: tuple[Debt, ...] = (),
    steps: tuple[StepContract, ...] = (),
) -> ModelView:
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
        steps=steps,
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


def test_plan_first_finishes_with_the_working_set() -> None:
    step = StepContract(
        id=1,
        kind=StepKind.INSPECT,
        intent=ToolIntent(capability=Capability.READ),
    )
    view = _view(steps=(step,))
    strategy = PlanFirst(
        lambda _view: PlanProposal(steps=(step,)),
        finisher=lambda _view: Finish("the answer"),
    )

    first = strategy.propose(view)
    assert isinstance(first, SubmitPlan)
    second = strategy.propose(view)
    assert isinstance(second, Finish)
    assert second.answer == "the answer"


def test_reflect_first_asks_for_one_correction_before_finishing() -> None:
    strategy = ReflectFirst(
        _ActThenFinish(),
        lambda view: CallTool(ToolIntent(capability=Capability.EDIT)),
    )

    assert isinstance(strategy.propose(_view()), CallTool)
    # The finish attempt is intercepted once by the reflector.
    assert isinstance(strategy.propose(_view()), CallTool)
    assert isinstance(strategy.propose(_view()), Finish)


def test_reflect_first_is_bounded() -> None:
    strategy = ReflectFirst(
        _ActThenFinish(),
        lambda view: CallTool(ToolIntent(capability=Capability.EDIT)),
        max_reflections=2,
    )

    assert isinstance(strategy.propose(_view()), CallTool)
    assert isinstance(strategy.propose(_view()), CallTool)
    assert isinstance(strategy.propose(_view()), CallTool)
    assert isinstance(strategy.propose(_view()), Finish)


def test_reflect_first_never_runs_without_prior_action() -> None:
    calls = {"reflect": 0}

    def reflector(view: ModelView):
        calls["reflect"] += 1
        return CallTool(ToolIntent(capability=Capability.EDIT))

    strategy = ReflectFirst(ActFirst(lambda view: Finish("done")), reflector)

    assert isinstance(strategy.propose(_view()), Finish)
    assert calls["reflect"] == 0, "没有行动就不该反思"


def test_reflect_first_cannot_finish_or_ask_on_its_own() -> None:
    strategy = ReflectFirst(
        _ActThenFinish(),
        lambda view: Finish("I reviewed it and it is fine"),
    )
    assert isinstance(strategy.propose(_view()), CallTool)
    # A reflector that declares success is discarded; completion stays with
    # the delegate, and verification stays deterministic.
    assert isinstance(strategy.propose(_view()), Finish)
    assert strategy._reflections == 1


class _ActThenFinish:
    """Deterministic double: one tool call, then finish."""

    def __init__(self) -> None:
        self._acted = False

    def propose(self, view: ModelView):
        if not self._acted:
            self._acted = True
            return CallTool(ToolIntent(capability=Capability.READ))
        return Finish("done")


def test_reflect_first_reports_what_it_decided() -> None:
    strategy = ReflectFirst(
        _ActThenFinish(),
        lambda view: CallTool(ToolIntent(capability=Capability.EDIT)),
    )

    strategy.propose(_view())
    assert strategy.drain_notices() == ()

    strategy.propose(_view())
    notices = strategy.drain_notices()

    assert notices == ({"decision": "correction", "capability": "edit", "round": 1},)
    assert strategy.drain_notices() == (), "通知是排空的，不能重复上报"


def test_reflect_first_records_a_declined_review() -> None:
    strategy = ReflectFirst(
        _ActThenFinish(),
        lambda view: Finish("looks fine"),
    )

    strategy.propose(_view())
    strategy.propose(_view())

    assert strategy.drain_notices() == (
        {"decision": "declined", "capability": "", "round": 1},
    )
