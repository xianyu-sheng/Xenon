"""The loop seam: plan, react and reflect are phases, not classes (#198)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from xenon.kernel.contracts import (
    AskUser,
    CallTool,
    Capability,
    Claim,
    ClaimSource,
    Debt,
    Finish,
    ModelView,
    Obligation,
    ObligationSource,
    PlanProposal,
    PropositionKind,
    ReleaseStep,
    StepContract,
    StepKind,
    SubmitPlan,
    TaskSpec,
    Think,
    ToolIntent,
)
from xenon.kernel.strategies import (
    LoopProfile,
    LoopStrategy,
    NoToolStrategy,
    RepairFirst,
)


def _view(
    *,
    debt: tuple[Debt, ...] = (),
    steps: tuple[StepContract, ...] = (),
    active: StepContract | None = None,
) -> ModelView:
    spec = TaskSpec(task_id="t1", objective="x", workspace=".")
    return ModelView(
        task=spec,
        system="s",
        tools=(),
        working_set=(),
        debt=debt,
        budget=spec.budget,
        input_seq=0,
        hash="h",
        active_step=active,
        steps=steps,
    )


class _ActThenFinish:
    """One tool call, then finish."""

    def __init__(self) -> None:
        self._acted = False

    def propose(self, view: ModelView):
        if not self._acted:
            self._acted = True
            return CallTool(ToolIntent(capability=Capability.READ))
        return Finish("done")


def _plan_step(step_id: int = 1, *, expects: tuple[Obligation, ...] = ()) -> StepContract:
    return StepContract(
        id=step_id,
        kind=StepKind.EDIT,
        intent=ToolIntent(capability=Capability.EDIT),
        targets=("a.py",),
        expects=expects,
    )


def _obligation(name: str = "e1") -> Obligation:
    return Obligation(
        id=name,
        kind=PropositionKind.FILE_CHANGED,
        target="a.py",
        source=ObligationSource.PLAN,
    )


# ── profiles ───────────────────────────────────────────────────────────


def test_profile_rejects_incoherent_budgets() -> None:
    with pytest.raises(ValueError, match="step_calls"):
        LoopProfile(step_calls=0)
    with pytest.raises(ValueError, match="reflect_calls"):
        LoopProfile(reflect_rounds=2)
    with pytest.raises(ValueError, match="follow_plan"):
        LoopProfile(follow_plan=True)
    with pytest.raises(ValueError, match="step_reflect"):
        LoopProfile(step_reflect=True)


def test_profile_requires_the_providers_it_enables() -> None:
    with pytest.raises(ValueError, match="planner"):
        LoopStrategy(LoopProfile(plan=True), actor=lambda view: Finish("x"))
    with pytest.raises(ValueError, match="step_actor"):
        LoopStrategy(
            LoopProfile(plan=True, step_scope=True),
            actor=lambda view: Finish("x"),
            planner=lambda view: PlanProposal(steps=()),
        )
    with pytest.raises(ValueError, match="reflector"):
        LoopStrategy(
            LoopProfile(reflect_rounds=1, reflect_calls=1),
            actor=lambda view: Finish("x"),
        )


# ── react phase ────────────────────────────────────────────────────────


def test_act_only_profile_is_the_bare_react_phase() -> None:
    strategy = LoopStrategy(LoopProfile(), actor=lambda view: Finish("hi"))

    assert isinstance(strategy.propose(_view()), Finish)


def test_plan_examples_submit_once_then_follow_the_plan() -> None:
    plan = PlanProposal(steps=(_plan_step(),))
    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True, follow_plan=True),
        actor=lambda view: Finish("done"),
        planner=lambda view: plan,
    )
    step = _plan_step()

    assert isinstance(strategy.propose(_view()), SubmitPlan)
    action = strategy.propose(_view(steps=(step,), active=step))

    assert isinstance(action, CallTool)
    assert action.intent.capability is Capability.EDIT


def test_follow_plan_thinks_when_a_step_has_no_intent() -> None:
    step = StepContract(id=1, kind=StepKind.INSPECT)
    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True, follow_plan=True),
        actor=lambda view: Finish("done"),
        planner=lambda view: PlanProposal(steps=(step,)),
    )
    strategy.propose(_view())  # submit

    assert isinstance(strategy.propose(_view(steps=(step,), active=step)), Think)


def test_step_react_uses_a_per_step_budget() -> None:
    step = _plan_step()
    seen: list[int] = []

    def actor(view: ModelView, active: StepContract, calls: int):
        seen.append(active.id)
        return CallTool(ToolIntent(capability=Capability.READ))

    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True, step_calls=2),
        actor=lambda view: Finish("done"),
        step_actor=actor,
        planner=lambda view: PlanProposal(steps=(step,)),
    )
    strategy.propose(_view())  # submit

    assert isinstance(strategy.propose(_view(steps=(step,), active=step)), CallTool)
    assert isinstance(strategy.propose(_view(steps=(step,), active=step)), CallTool)
    released = strategy.propose(_view(steps=(step,), active=step))

    assert isinstance(released, ReleaseStep)
    assert seen == [1, 1]
    assert strategy.drain_notices()[0] == {
        "kind": "step",
        "decision": "budget_exhausted",
        "step": 1,
        "calls": 2,
    }


def test_step_actor_finishing_releases_the_step_not_the_task() -> None:
    step = _plan_step()
    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True),
        actor=lambda view: Finish("done"),
        step_actor=lambda view, active, calls: Finish("step done"),
        planner=lambda view: PlanProposal(steps=(step,)),
    )
    strategy.propose(_view())

    action = strategy.propose(_view(steps=(step,), active=step))

    assert isinstance(action, ReleaseStep)
    assert strategy.drain_notices()[0]["decision"] == "step_complete"


# ── plan review ────────────────────────────────────────────────────────


def test_plan_review_can_add_coverage() -> None:
    original = PlanProposal(steps=(_plan_step(expects=(_obligation(),)),))
    extra = StepContract(id=2, kind=StepKind.VERIFY, targets=("tests/a.py",))
    strategy = LoopStrategy(
        LoopProfile(plan=True, plan_review=True, step_scope=True, follow_plan=True),
        actor=lambda view: Finish("done"),
        planner=lambda view: original,
        plan_reviewer=lambda view, plan: PlanProposal(
            steps=original.steps + (extra,)
        ),
    )

    action = strategy.propose(_view())

    assert isinstance(action, SubmitPlan)
    assert [step.id for step in action.plan.steps] == [1, 2]
    assert strategy.drain_notices()[0] == {
        "kind": "plan",
        "decision": "amended",
        "added_steps": 1,
    }


def test_plan_review_cannot_drop_an_obligation() -> None:
    original = PlanProposal(steps=(_plan_step(expects=(_obligation(),)),))
    weaker = PlanProposal(steps=(_plan_step(expects=()),))
    strategy = LoopStrategy(
        LoopProfile(plan=True, plan_review=True, step_scope=True, follow_plan=True),
        actor=lambda view: Finish("done"),
        planner=lambda view: original,
        plan_reviewer=lambda view, plan: weaker,
    )

    action = strategy.propose(_view())

    assert isinstance(action, SubmitPlan)
    assert action.plan.steps[0].expects, "审查不能削掉义务"
    notice = strategy.drain_notices()[0]
    assert notice["decision"] == "rejected"
    assert "e1" in notice["reason"]


def test_plan_review_declining_keeps_the_plan() -> None:
    original = PlanProposal(steps=(_plan_step(),))
    strategy = LoopStrategy(
        LoopProfile(plan=True, plan_review=True, step_scope=True, follow_plan=True),
        actor=lambda view: Finish("done"),
        planner=lambda view: original,
        plan_reviewer=lambda view, plan: None,
    )

    strategy.propose(_view())

    assert strategy.drain_notices()[0] == {"kind": "plan", "decision": "accepted"}


# ── step review ────────────────────────────────────────────────────────


def test_step_review_adds_one_correction_before_release() -> None:
    step = _plan_step()
    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True, step_reflect=True),
        actor=lambda view: Finish("done"),
        step_actor=lambda view, active, calls: Finish("step done"),
        planner=lambda view: PlanProposal(steps=(step,)),
        step_reviewer=lambda view, active, action: CallTool(
            ToolIntent(capability=Capability.VERIFY)
        ),
    )
    strategy.propose(_view())

    first = strategy.propose(_view(steps=(step,), active=step))
    second = strategy.propose(_view(steps=(step,), active=step))

    assert isinstance(first, CallTool)
    assert isinstance(second, ReleaseStep)
    assert [item["decision"] for item in strategy.drain_notices()] == [
        "step_correction",
        "step_complete",
    ]


def test_step_review_declining_releases_the_step() -> None:
    step = _plan_step()
    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True, step_reflect=True),
        actor=lambda view: Finish("done"),
        step_actor=lambda view, active, calls: Finish("step done"),
        planner=lambda view: PlanProposal(steps=(step,)),
        step_reviewer=lambda view, active, action: None,
    )
    strategy.propose(_view())

    assert isinstance(
        strategy.propose(_view(steps=(step,), active=step)), ReleaseStep
    )
    assert strategy.drain_notices()[0]["decision"] == "step_review_declined"


# ── reflect phase ──────────────────────────────────────────────────────


def test_reflection_runs_one_round_by_default() -> None:
    strategy = LoopStrategy(
        LoopProfile(reflect_rounds=1, reflect_calls=1),
        actor=_ActThenFinish().propose,
        reflector=lambda view, finish: CallTool(
            ToolIntent(capability=Capability.EDIT)
        ),
    )

    assert isinstance(strategy.propose(_view()), CallTool)  # act
    assert isinstance(strategy.propose(_view()), CallTool)  # reviewed correction
    assert isinstance(strategy.propose(_view()), Finish)
    assert strategy.drain_notices()[0]["decision"] == "correction"


def test_reflection_never_runs_without_prior_action() -> None:
    calls: list[int] = []

    def reflector(view: ModelView, finish: Finish):
        calls.append(1)
        return CallTool(ToolIntent(capability=Capability.EDIT))

    strategy = LoopStrategy(
        LoopProfile(reflect_rounds=2, reflect_calls=1),
        actor=lambda view: Finish("nothing to do"),
        reflector=reflector,
    )

    assert isinstance(strategy.propose(_view()), Finish)
    assert calls == [], "没有行动就不该反思"


def test_reflection_rounds_and_per_round_budget_are_bounded() -> None:
    capabilities = [
        Capability.EDIT,
        Capability.VERIFY,
        Capability.READ,
        Capability.SHELL,
        Capability.SEARCH,
        Capability.VCS,
    ]
    used: list[int] = []

    def reflector(view: ModelView, finish: Finish):
        capability = capabilities[len(used)]
        used.append(1)
        return CallTool(ToolIntent(capability=capability))

    strategy = LoopStrategy(
        LoopProfile(reflect_rounds=2, reflect_calls=2),
        actor=_ActThenFinish().propose,
        reflector=reflector,
    )

    actions = [strategy.propose(_view()) for _ in range(10)]

    assert len(used) == 4, "2 轮 × 每轮 2 次"
    assert isinstance(actions[-1], Finish)
    assert [item["round"] for item in strategy.drain_notices()] == [1, 1, 2, 2]


def test_repeated_correction_stops_reflection() -> None:
    strategy = LoopStrategy(
        LoopProfile(reflect_rounds=5, reflect_calls=1),
        actor=_ActThenFinish().propose,
        reflector=lambda view, finish: CallTool(
            ToolIntent(capability=Capability.EDIT)
        ),
    )

    actions = [strategy.propose(_view()) for _ in range(6)]

    assert [item["decision"] for item in strategy.drain_notices()] == [
        "correction",
        "repeated",
    ]
    assert isinstance(actions[-1], Finish)


def test_declined_review_ends_all_remaining_rounds() -> None:
    reviews: list[int] = []

    def reflector(view: ModelView, finish: Finish):
        reviews.append(1)
        return None

    strategy = LoopStrategy(
        LoopProfile(reflect_rounds=3, reflect_calls=2),
        actor=_ActThenFinish().propose,
        reflector=reflector,
    )

    actions = [strategy.propose(_view()) for _ in range(5)]

    assert len(reviews) == 1
    assert isinstance(actions[-1], Finish)
    assert strategy.drain_notices()[0]["decision"] == "declined"


def test_reflection_sees_the_proposed_answer_and_claims() -> None:
    captured: dict[str, object] = {}
    claim = Claim(
        id="c1",
        kind=PropositionKind.FILE_CHANGED,
        target="a.py",
        source=ClaimSource.LLM,
    )

    def reflector(view: ModelView, finish: Finish):
        captured["answer"] = finish.answer
        captured["claims"] = finish.claims
        return None

    actor = _ActThenFinishThenClaim(claim)
    strategy = LoopStrategy(
        LoopProfile(reflect_rounds=1, reflect_calls=1),
        actor=actor.propose,
        reflector=reflector,
    )
    strategy.propose(_view())
    strategy.propose(_view())

    assert captured["claims"] == (claim,)


class _ActThenFinishThenClaim:
    """One tool call, then a finish carrying a claim."""

    def __init__(self, claim: Claim) -> None:
        self.claim = claim
        self._acted = False

    def propose(self, view: ModelView):
        if not self._acted:
            self._acted = True
            return CallTool(ToolIntent(capability=Capability.READ))
        return Finish("done", claims=(self.claim,))


# ── other strategies ───────────────────────────────────────────────────


def test_no_tool_strategy_finishes_without_evidence() -> None:
    claim = Claim(
        id="c1",
        kind=PropositionKind.FILE_CHANGED,
        target="a.py",
        source=ClaimSource.LLM,
    )
    strategy = NoToolStrategy("hello", claims=(claim,))

    action = strategy.propose(_view())

    assert isinstance(action, Finish)
    assert action.claims == (claim,)


def test_repair_first_only_acts_when_debt_is_open() -> None:
    obligation = _obligation()
    debt = Debt(obligation=obligation, reason="missing")
    strategy = RepairFirst(
        fallback=SimpleNamespace(propose=lambda view: Finish("fallback")),
        repairer=lambda view, debts: CallTool(ToolIntent(capability=Capability.EDIT)),
    )

    assert isinstance(strategy.propose(_view()), Finish)
    assert isinstance(strategy.propose(_view(debt=(debt,))), CallTool)


def test_notices_are_drained_once() -> None:
    strategy = LoopStrategy(
        LoopProfile(reflect_rounds=1, reflect_calls=1),
        actor=_ActThenFinish().propose,
        reflector=lambda view, finish: None,
    )
    strategy.propose(_view())
    strategy.propose(_view())

    assert len(strategy.drain_notices()) == 1
    assert strategy.drain_notices() == ()


def test_ask_user_inside_a_step_is_preserved() -> None:
    step = _plan_step()
    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True),
        actor=lambda view: Finish("done"),
        step_actor=lambda view, active, calls: AskUser("which file?"),
        planner=lambda view: PlanProposal(steps=(step,)),
    )
    strategy.propose(_view())

    assert isinstance(strategy.propose(_view(steps=(step,), active=step)), AskUser)
