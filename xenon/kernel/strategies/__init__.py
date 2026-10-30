"""One loop, three phases: plan → react → reflect (#198, consolidated).

The kernel has exactly one strategy shape.  ``LoopProfile`` decides which phases
run; it never selects a different class.  That is deliberate: seven thinking
modes must not become seven strategy implementations again.

* **plan**       — propose one plan, optionally review its coverage first;
* **react**      — work the active step in a bounded sub-loop (or follow the
  plan's own intents when ``follow_plan`` is set);
* **reflect**    — review the proposed finish, and optionally the proposed step
  release, in bounded rounds.

Everything here only *proposes*.  Tools are executed by the gateway, steps are
closed by the runtime, obligations are verified by the controller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from xenon.kernel.contracts import (
    Abort,
    Action,
    AskUser,
    CallTool,
    Claim,
    Debt,
    Finish,
    ModelView,
    PlanProposal,
    ReleaseStep,
    StepContract,
    SubmitPlan,
    Think,
)

PlanProvider = Callable[[ModelView], PlanProposal]
ActionProvider = Callable[[ModelView], Action]
RepairProvider = Callable[[ModelView, tuple[Debt, ...]], Action]
StepActor = Callable[[ModelView, Any, int], Action]
PlanReviewer = Callable[[ModelView, PlanProposal], PlanProposal | None]
ReflectionProvider = Callable[[ModelView, Finish], Action | None]
StepReviewer = Callable[[ModelView, Any, Action], Action | None]


@dataclass(frozen=True, slots=True)
class LoopProfile:
    """Which phases of the single loop a mode enables.

    Attributes:
        plan: Propose a plan before acting.
        plan_review: Review plan coverage before the plan is submitted.
        step_scope: Run the plan one step at a time with a per-step sub-loop.
        follow_plan: Execute the plan's own intents instead of asking the actor
            what to do next (deterministic execution, no per-step model call).
        step_calls: Call budget for one step (or for one turn without steps).
        step_reflect: Review a step before it is released.
        reflect_rounds: Review rounds at finish.  ``0`` disables reflection.
        reflect_calls: Corrective calls allowed per review round.
    """

    plan: bool = False
    plan_review: bool = False
    step_scope: bool = False
    follow_plan: bool = False
    step_calls: int = 8
    step_reflect: bool = False
    reflect_rounds: int = 0
    reflect_calls: int = 0

    def __post_init__(self) -> None:
        if self.step_calls < 1:
            raise ValueError("step_calls must be >= 1")
        if self.reflect_rounds < 0 or self.reflect_calls < 0:
            raise ValueError("reflection budgets must be >= 0")
        if self.reflect_rounds and not self.reflect_calls:
            raise ValueError("reflect_rounds requires reflect_calls")
        if self.follow_plan and not self.plan:
            raise ValueError("follow_plan requires a plan phase")
        if self.step_reflect and not self.step_scope:
            raise ValueError("step_reflect requires step_scope")

    @property
    def reflects(self) -> bool:
        return bool(self.reflect_rounds and self.reflect_calls)


@dataclass(slots=True)
class LoopStrategy:
    """The only kernel strategy.  ``profile`` decides which phases run."""

    profile: LoopProfile
    actor: ActionProvider
    step_actor: StepActor | None = None
    planner: PlanProvider | None = None
    plan_reviewer: PlanReviewer | None = None
    reflector: ReflectionProvider | None = None
    step_reviewer: StepReviewer | None = None
    finisher: ActionProvider | None = None
    finish_text: str = "completed"
    resumed: bool = False
    _submitted: bool = False
    _finished: bool = False
    _step_id: int | None = None
    _step_calls: int = 0
    _step_reviews: int = 0
    _round: int = 0
    _round_calls: int = 0
    _acted: bool = False
    _stopped: bool = False
    _seen: set[str] = field(default_factory=set)
    _notices: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.profile.plan and self.planner is None and not self.resumed:
            raise ValueError("a plan profile needs a planner")
        if self.profile.plan_review and self.plan_reviewer is None:
            raise ValueError("plan_review needs a plan_reviewer")
        if self.profile.reflects and self.reflector is None:
            raise ValueError("reflection needs a reflector")
        if self.profile.step_reflect and self.step_reviewer is None:
            raise ValueError("step_reflect needs a step_reviewer")
        if (
            self.profile.step_scope
            and not self.profile.follow_plan
            and self.step_actor is None
        ):
            raise ValueError("a step-scoped profile needs a step_actor")

    # ── proposal ────────────────────────────────────────────────────────

    def propose(self, view: ModelView) -> Action:
        if self.profile.plan and not self._submitted and not self.resumed:
            self._submitted = True
            return SubmitPlan(self._review_plan(view, self.planner(view)))
        if self.profile.step_scope and view.active_step is not None:
            return self._step_turn(view)
        if self.profile.plan and view.active_step is None and not view.steps:
            # An empty/invalid plan must not trigger a model call.
            return Finish(self.finish_text)
        return self._reflect_or_finish(view, self._task_action(view))

    def _task_action(self, view: ModelView) -> Action:
        if self.profile.plan and view.steps and self.finisher and not self._finished:
            self._finished = True
            return self.finisher(view)
        return self.actor(view)

    # ── plan phase ──────────────────────────────────────────────────────

    def _review_plan(self, view: ModelView, plan: PlanProposal) -> PlanProposal:
        if not self.profile.plan_review or self.plan_reviewer is None:
            return plan
        revised = self.plan_reviewer(view, plan)
        if revised is None:
            self._notices.append({"kind": "plan", "decision": "accepted"})
            return plan
        missing = _missing_obligations(plan, revised)
        if missing:
            # A review may add coverage, never remove it.
            self._notices.append(
                {
                    "kind": "plan",
                    "decision": "rejected",
                    "reason": "would drop obligations: " + ", ".join(missing),
                }
            )
            return plan
        self._notices.append(
            {
                "kind": "plan",
                "decision": "amended",
                "added_steps": len(revised.steps) - len(plan.steps),
            }
        )
        return revised

    # ── react phase ─────────────────────────────────────────────────────

    def _step_turn(self, view: ModelView) -> Action:
        active = view.active_step
        assert active is not None
        if self._step_id != active.id:
            self._step_id = active.id
            self._step_calls = 0
            self._step_reviews = 0
        if self.profile.follow_plan:
            return self._follow_plan(active)
        if self._step_calls >= self.profile.step_calls:
            # Bounded exploration: stop spending calls on this step.  Releasing
            # closes it deterministically, so an unmet expectation becomes debt
            # instead of a delivery.
            self._notices.append(
                {
                    "kind": "step",
                    "decision": "budget_exhausted",
                    "step": active.id,
                    "calls": self._step_calls,
                }
            )
            return ReleaseStep(f"step {active.id}: step budget exhausted")
        # Every iteration costs budget, including notes and invalid proposals,
        # so a confused actor cannot spin forever.
        self._step_calls += 1
        action = self.step_actor(view, active, self._step_calls)
        if isinstance(action, (Think, AskUser, Abort, CallTool)):
            return action
        return self._release_step(view, active)

    def _follow_plan(self, step: StepContract) -> Action:
        if step.intent is None:
            return Think(f"step {step.id}: {step.kind.value}")
        intent = step.intent
        if step.prefer and intent.prefer is None:
            intent = replace(intent, prefer=step.prefer)
        return CallTool(intent)

    def _release_step(self, view: ModelView, step: StepContract) -> Action:
        """Review the step before it is released, then release it."""
        reviewed = self._review_step(view, step)
        if isinstance(reviewed, CallTool):
            return reviewed
        self._notices.append(
            {
                "kind": "step",
                "decision": "step_complete",
                "step": step.id,
                "calls": self._step_calls,
            }
        )
        return ReleaseStep(f"step {step.id}: sub-loop complete")

    def _review_step(self, view: ModelView, step: StepContract) -> Action | None:
        if not self.profile.step_reflect or self.step_reviewer is None:
            return None
        if self._step_reviews >= 1:
            return None
        self._step_reviews += 1
        proposal = self.step_reviewer(view, step, Finish("step complete"))
        if isinstance(proposal, CallTool):
            self._notices.append(
                {
                    "kind": "step",
                    "decision": "step_correction",
                    "step": step.id,
                    "capability": proposal.intent.capability.value,
                    "review": self._step_reviews,
                }
            )
            return proposal
        self._notices.append(
            {
                "kind": "step",
                "decision": "step_review_declined",
                "step": step.id,
                "review": self._step_reviews,
            }
        )
        return None

    # ── reflect phase ───────────────────────────────────────────────────

    def _reflect_or_finish(self, view: ModelView, action: Action) -> Action:
        if isinstance(action, CallTool):
            self._acted = True
            return action
        if not isinstance(action, Finish):
            return action
        if not self.profile.reflects or self.reflector is None:
            return action
        if not self._acted or self._stopped:
            return action
        if self._round == 0:
            self._round = 1
            self._round_calls = 0
        elif self._round_calls >= self.profile.reflect_calls:
            # This round spent its budget; a new round may review again.
            self._round += 1
            self._round_calls = 0
        if self._round > self.profile.reflect_rounds:
            return action
        proposal = self.reflector(view, action)
        if isinstance(proposal, CallTool):
            signature = _correction_signature(proposal)
            if signature in self._seen:
                # Repeating the same correction cannot make progress.
                self._stopped = True
                self._notices.append(
                    {
                        "kind": "reflection",
                        "decision": "repeated",
                        "capability": proposal.intent.capability.value,
                        "round": self._round,
                    }
                )
                return action
            self._seen.add(signature)
            self._round_calls += 1
            self._acted = True
            self._notices.append(
                {
                    "kind": "reflection",
                    "decision": "correction",
                    "capability": proposal.intent.capability.value,
                    "round": self._round,
                    "call_in_round": self._round_calls,
                }
            )
            return proposal
        # A reviewer that finds nothing left also ends reflection: more rounds
        # would only re-read the same receipts.
        self._stopped = True
        self._notices.append(
            {
                "kind": "reflection",
                "decision": "declined",
                "capability": "",
                "round": self._round,
            }
        )
        return action

    # ── audit trail ─────────────────────────────────────────────────────

    def drain_notices(self) -> tuple[dict[str, Any], ...]:
        """Return pending phase decisions for ledger auditing.

        The strategy cannot write the ledger itself, so it hands the runtime a
        short, append-only record.  This keeps the phases observable without
        giving any of them authority.
        """

        notices = tuple(self._notices)
        self._notices.clear()
        return notices


def _correction_signature(action: CallTool) -> str:
    """Stable identity of a proposed correction, for loop detection."""

    intent = action.intent
    return json.dumps(
        [intent.capability.value, intent.params, list(intent.targets)],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _missing_obligations(
    original: PlanProposal, revised: PlanProposal
) -> tuple[str, ...]:
    """Obligations present in the original plan but absent from the revision."""

    kept: dict[int, set[str]] = {}
    for step in revised.steps:
        kept.setdefault(step.id, set()).update(
            obligation.id for obligation in step.expects
        )
    missing: list[str] = []
    for step in original.steps:
        for obligation in step.expects:
            if obligation.id not in kept.get(step.id, set()):
                missing.append(obligation.id)
    return tuple(missing)


@dataclass(slots=True)
class RepairFirst:
    """Let a bounded repair adapter act while debt is open."""

    fallback: Any  # Strategy protocol; Any avoids a circular import
    repairer: RepairProvider

    def propose(self, view: ModelView) -> Action:
        if view.debt:
            return self.repairer(view, view.debt)
        return self.fallback.propose(view)


@dataclass(slots=True)
class NoToolStrategy:
    """Chat-only strategy: answer without pretending to have evidence."""

    answer: str
    claims: tuple[Claim, ...] = ()

    def propose(self, view: ModelView) -> Action:
        return Finish(self.answer, claims=self.claims)


__all__ = [
    "ActionProvider",
    "LoopProfile",
    "LoopStrategy",
    "NoToolStrategy",
    "PlanProvider",
    "PlanReviewer",
    "ReflectionProvider",
    "RepairFirst",
    "RepairProvider",
    "StepActor",
    "StepReviewer",
]
