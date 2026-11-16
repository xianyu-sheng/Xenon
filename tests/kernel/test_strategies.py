"""The loop seam: plan, react and reflect are phases, not classes (#198)."""

from __future__ import annotations

from pathlib import Path
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
    RequestEscalation,
    StepContract,
    StepKind,
    SubmitPlan,
    TaskSpec,
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
    workspace: str = ".",
) -> ModelView:
    spec = TaskSpec(task_id="t1", objective="x", workspace=workspace)
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


def test_follow_plan_releases_a_step_with_no_intent() -> None:
    """A follow-plan step is executed by its bound capability.

    Without one there is nothing to run.  Emitting a thought (the old
    behaviour) burned the repair budget on a step that could never make
    progress, so the step is released with a reason instead.
    """

    step = StepContract(id=1, kind=StepKind.INSPECT)
    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True, follow_plan=True),
        actor=lambda view: Finish("done"),
        planner=lambda view: PlanProposal(steps=(step,)),
    )
    strategy.propose(_view())  # submit

    action = strategy.propose(_view(steps=(step,), active=step))

    assert isinstance(action, ReleaseStep)
    assert "no bound capability" in action.reason


def test_follow_plan_issues_its_bound_action_exactly_once() -> None:
    """An approved plan must not replay a failed step forever.

    Measured C10 approval run: the plan bound ``read`` to a directory.  Every
    round re-issued the identical call; after seven attempts the stuck
    detector blocked the *whole* task, so the edit step behind it never ran.
    A follow-plan step has one bound action — repeating it cannot differ
    from the first attempt — so the strategy releases the step and lets the
    runtime move on.
    """

    step = _plan_step()
    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True, follow_plan=True),
        actor=lambda view: Finish("done"),
        planner=lambda view: PlanProposal(steps=(step,)),
    )
    strategy.propose(_view())  # submit
    view = _view(steps=(step,), active=step)

    assert isinstance(strategy.propose(view), CallTool)
    released = strategy.propose(view)

    assert isinstance(released, ReleaseStep)
    assert "already executed" in released.reason


def test_follow_plan_runs_a_changed_intent() -> None:
    """A replan may reuse a step id with a genuinely different action."""

    step = _plan_step()
    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True, follow_plan=True),
        actor=lambda view: Finish("done"),
        planner=lambda view: PlanProposal(steps=(step,)),
    )
    strategy.propose(_view())
    revised = _plan_step()
    revised = StepContract(
        id=revised.id,
        kind=revised.kind,
        intent=ToolIntent(capability=Capability.EDIT, params={"file_path": "b.py"}),
        targets=("b.py",),
    )

    assert isinstance(strategy.propose(_view(steps=(step,), active=step)), CallTool)
    action = strategy.propose(_view(steps=(revised,), active=revised))

    assert isinstance(action, CallTool)
    assert action.intent.params == {"file_path": "b.py"}


def _write_step(path: str, content: str, *, step_id: int = 1) -> StepContract:
    return StepContract(
        id=step_id,
        kind=StepKind.EDIT,
        intent=ToolIntent(
            capability=Capability.EDIT,
            params={"files": [{"path": path, "content": content}]},
        ),
        targets=(path,),
    )


def test_follow_plan_replays_a_write_that_creates_a_new_file(
    tmp_path: Path,
) -> None:
    """Creating a path needs no observation: authoring it is the plan's job."""

    step = _write_step("new.py", "x = 1\n")
    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True, follow_plan=True),
        actor=lambda view: Finish("done"),
        planner=lambda view: PlanProposal(steps=(step,)),
    )
    strategy.propose(_view(workspace=str(tmp_path)))

    action = strategy.propose(
        _view(steps=(step,), active=step, workspace=str(tmp_path))
    )

    assert isinstance(action, CallTool)
    assert action.intent.params["files"][0]["content"] == "x = 1\n"


def test_follow_plan_materializes_a_write_with_no_payload(
    tmp_path: Path,
) -> None:
    """A demoted edit step describes work; only the actor can perform it."""

    step = StepContract(
        id=1,
        kind=StepKind.EDIT,
        intent=ToolIntent(capability=Capability.EDIT),
        targets=("calc.py",),
    )
    seen: list[int] = []

    def actor(view: ModelView, active: StepContract, calls: int):
        seen.append(active.id)
        return CallTool(
            ToolIntent(capability=Capability.EDIT, params={"files": []})
        )

    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True, follow_plan=True),
        actor=lambda view: Finish("done"),
        step_actor=actor,
        planner=lambda view: PlanProposal(steps=(step,)),
        resumed=True,
    )

    action = strategy.propose(
        _view(steps=(step,), active=step, workspace=str(tmp_path))
    )

    assert isinstance(action, CallTool)
    assert seen == [1]


def test_approved_plan_never_replays_a_write_over_an_existing_file(
    tmp_path: Path,
) -> None:
    """The plan phase saw no receipts, so its file content is a guess.

    Measured C10 approval run: the plan bound ``apply_patch`` with
    ``"<minimal fix of the buggy function body ...>"`` as the content of the
    real ``buggy_calc.py``.  Following it verbatim overwrote the file with the
    placeholder and the verify step then ran pytest against the wreckage.
    """

    (tmp_path / "calc.py").write_text(
        "def f():\n    raise ValueError\n", encoding="utf-8"
    )
    step = _write_step("calc.py", "<minimal fix of the body>")
    seen: list[int] = []

    def actor(view: ModelView, active: StepContract, calls: int):
        seen.append(active.id)
        return CallTool(
            ToolIntent(capability=Capability.READ, params={"file_path": "calc.py"})
        )

    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True, follow_plan=True),
        actor=lambda view: Finish("done"),
        step_actor=actor,
        planner=lambda view: PlanProposal(steps=(step,)),
        resumed=True,
    )

    action = strategy.propose(
        _view(steps=(step,), active=step, workspace=str(tmp_path))
    )

    assert isinstance(action, CallTool)
    # The actor's move, not the plan's bound payload.
    assert action.intent.capability is Capability.READ
    assert seen == [1]
    assert (
        tmp_path / "calc.py"
    ).read_text(encoding="utf-8").startswith("def f()")


def test_approved_plan_releases_an_ungrounded_write_without_an_actor(
    tmp_path: Path,
) -> None:
    (tmp_path / "calc.py").write_text("x = 1\n", encoding="utf-8")
    step = _write_step("calc.py", "<placeholder>")
    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True, follow_plan=True),
        actor=lambda view: Finish("done"),
        planner=lambda view: PlanProposal(steps=(step,)),
        resumed=True,
    )

    action = strategy.propose(
        _view(steps=(step,), active=step, workspace=str(tmp_path))
    )

    assert isinstance(action, ReleaseStep)
    assert "could not have read" in action.reason
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == "x = 1\n"


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


def test_step_review_can_ask_the_human_for_a_refused_call() -> None:
    """A review is a decision with a form of appeal, not only "one more call".

    Real run: the bound step call was vetoed by the read-only boundary, the
    reviewer proposed another refused command, the step closed, and the model
    answered a number it could not verify ("这里没有弹出授权").  The reviewer's
    ask has to leave the step sub-loop exactly like a tool call.
    """

    step = _plan_step()
    intent = ToolIntent(
        capability=Capability.SHELL, params={"command": "Get-ChildItem *.py"}
    )
    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True, step_reflect=True),
        actor=lambda view: Finish("done"),
        step_actor=lambda view, active, calls: Finish("step done"),
        planner=lambda view: PlanProposal(steps=(step,)),
        step_reviewer=lambda view, active, action: RequestEscalation(
            intent, "counting files needs a directory listing"
        ),
    )
    strategy.propose(_view())

    proposed = strategy.propose(_view(steps=(step,), active=step))

    assert isinstance(proposed, RequestEscalation)
    assert proposed.intent.params == {"command": "Get-ChildItem *.py"}
    assert strategy.drain_notices()[0]["decision"] == "step_correction"


def test_a_step_actor_may_ask_for_a_refused_call() -> None:
    """``RequestEscalation`` belongs beside ``CallTool`` in the step sub-loop."""
    step = _plan_step()
    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True),
        actor=lambda view: Finish("done"),
        step_actor=lambda view, active, calls: RequestEscalation(
            ToolIntent(capability=Capability.READ), "needs one read"
        ),
        planner=lambda view: PlanProposal(steps=(step,)),
    )
    strategy.propose(_view())

    proposed = strategy.propose(_view(steps=(step,), active=step))

    assert isinstance(proposed, RequestEscalation)


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


def test_an_empty_plan_answers_instead_of_failing_the_turn() -> None:
    """A conversational task legitimately gets an empty plan.

    A real provider returned `steps: []` for "reply with ok" about one call in
    three; the turn then died with "no successful tool receipt" instead of
    answering.  The answer is still unverified — only the turn survives.
    """

    strategy = LoopStrategy(
        LoopProfile(plan=True),
        actor=lambda view: Finish("unused"),
        planner=lambda view: PlanProposal(steps=(), analysis="nothing to do"),
        finisher=lambda view: Finish("ok"),
    )

    action = strategy.propose(_view())
    # The empty plan is recorded (the ledger must show why nothing ran), and
    # the follow-up turn asks for the answer instead of ending the run.
    assert isinstance(action, SubmitPlan)
    assert action.plan.steps == ()

    answer = strategy.propose(_view())
    assert isinstance(answer, Finish)
    assert answer.answer == "ok"


def test_an_actor_that_dropped_calls_is_named_in_a_notice() -> None:
    """A parallel reply runs one action; the rest must reach the model.

    The strategy is where the actor's dropped calls meet the view, so it is
    where the note is raised.  It stays a notice: nothing is executed and no
    obligation moves.
    """

    class _Actor:
        skipped_calls = ("read({\"file_path\":\"util.py\"})",)

        def __call__(self, view):
            return CallTool(ToolIntent(capability=Capability.READ))

    strategy = LoopStrategy(LoopProfile(), actor=_Actor())

    strategy.propose(_view())

    notices = strategy.drain_notices()
    assert notices[0]["kind"] == "parallel"
    assert notices[0]["skipped"] == ["read({\"file_path\":\"util.py\"})"]


def test_a_plain_actor_raises_no_parallel_notice() -> None:
    strategy = LoopStrategy(LoopProfile(), actor=lambda view: Finish("hi"))

    strategy.propose(_view())

    assert strategy.drain_notices() == ()


def test_a_state_read_inside_a_step_is_passed_through() -> None:
    """Asking the harness is a move, not a step release.

    Measured: the step actor asked ``get_state what=session``; the strategy's
    allow-list did not include ``StateRead``, so the answer was rendered and
    discarded, the step was released, and the run finished claiming it had no
    session history.
    """

    from xenon.kernel.contracts import StateRead

    step = _plan_step()
    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True),
        actor=lambda view: Finish("done"),
        step_actor=lambda view, active, calls: StateRead("earlier tasks: 1"),
        planner=lambda view: PlanProposal(steps=(step,)),
    )
    strategy.propose(_view())

    action = strategy.propose(_view(steps=(step,), active=step))

    assert isinstance(action, StateRead)
    assert action.text == "earlier tasks: 1"
