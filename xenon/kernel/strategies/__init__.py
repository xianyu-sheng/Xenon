"""Small, stateless strategies for the single loop (#198).

A strategy only proposes; it never executes tools or mutates runtime state.
Plan/step state (active step, closure) lives in ``LoopRuntime``; a strategy may
hold only its own proposal cursor.
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
ReflectionProvider = Callable[[ModelView, Finish], Action | None]
StepActor = Callable[[ModelView, Any, int], Action]
PlanReviewer = Callable[[ModelView, PlanProposal], PlanProposal | None]
StepReviewer = Callable[[ModelView, Any, Action], Action | None]


@dataclass(slots=True)
class ActFirst:
    """Delegate action selection to a model/prompt adapter.

    The runtime remains responsible for binding, executing and verifying.  This
    class exists so the strategy seam is explicit and testable without a model.
    """

    proposer: ActionProvider

    def propose(self, view: ModelView) -> Action:
        return self.proposer(view)


@dataclass(slots=True)
class PlanFirst:
    """Submit one plan, then execute exactly one active step per proposal."""

    planner: PlanProvider
    finisher: ActionProvider | None = None
    finish_text: str = "completed"
    # Resume an already-accepted plan: skip SubmitPlan and act on the active
    # step the runtime reconstructed from the ledger.
    resumed: bool = False
    _submitted: bool = False
    _finished: bool = False

    def propose(self, view: ModelView) -> Action:
        if not self._submitted:
            self._submitted = True
            if not self.resumed:
                return SubmitPlan(self.planner(view))
        active = view.active_step
        if active is None:
            # A real plan should end with a synthesis over the step receipts.
            # Empty/invalid plans stay on the deterministic fallback so they
            # do not trigger an extra model call.
            if self.finisher is not None and view.steps and not self._finished:
                self._finished = True
                return self.finisher(view)
            return Finish(self.finish_text)
        if active.intent is None:
            return Think(f"step {active.id}: {active.kind.value}")
        intent = active.intent
        if active.prefer and intent.prefer is None:
            intent = replace(intent, prefer=active.prefer)
        return CallTool(intent)


@dataclass(slots=True)
class ReflectFirst:
    """Act, then let a bounded reviewer propose follow-up work.

    A reflector is a proposal source, not a judge.  It can only add tool calls;
    it can never mark a task complete, close debt, or turn an unverified claim
    into evidence.  Completion stays with the delegate's ``Finish`` and the
    controller's deterministic verification.

    Reflection is organised in rounds.  ``max_reflections`` bounds the number of
    reviews and ``max_calls_per_round`` bounds how much work one review may add,
    so ``max_reflections * max_calls_per_round`` is the hard ceiling of extra
    tool calls.  A reviewer that declines, or that repeats a correction it has
    already proposed, ends reflection immediately.
    """

    base: Any  # Strategy protocol; Any avoids a circular import
    reflector: ReflectionProvider
    max_reflections: int = 1
    max_calls_per_round: int = 1
    _round: int = 0
    _round_calls: int = 0
    _acted: bool = False
    _stopped: bool = False
    _seen: set[str] = field(default_factory=set)
    _notices: list[dict[str, Any]] = field(default_factory=list)

    def propose(self, view: ModelView) -> Action:
        action = self.base.propose(view)
        if isinstance(action, CallTool):
            self._acted = True
            return action
        if not isinstance(action, Finish):
            return action
        if not self._acted or self._stopped:
            return action
        if self._round == 0:
            self._round = 1
            self._round_calls = 0
        elif self._round_calls >= self.max_calls_per_round:
            # This round spent its budget; a new round may review again.
            self._round += 1
            self._round_calls = 0
        if self._round > self.max_reflections:
            return action
        proposal = self.reflector(view, action)
        if isinstance(proposal, CallTool):
            signature = _correction_signature(proposal)
            if signature in self._seen:
                # Repeating the same correction cannot make progress; stop
                # spending rounds on it and let verification decide.
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

    def drain_notices(self) -> tuple[dict[str, Any], ...]:
        """Return pending reflection decisions for ledger auditing.

        The strategy cannot write the ledger itself, so it hands the runtime a
        short, append-only record of what it decided.  This keeps reflection
        observable without giving it any authority.
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


@dataclass(slots=True)
class PlanReact:
    """Plan, review the plan, then run a reviewed bounded sub-loop per step.

    Three seams, one implementation:

    * ``planner``       — proposes the plan;
    * ``plan_reviewer`` — may amend the plan before execution, but only by
      adding steps: every original obligation must survive, so a review can
      never shrink what the task owes;
    * ``step_reviewer`` — may add work inside the active step before it is
      released, so a mistake is caught where it happened instead of after the
      whole plan has run.

    Reviewers are proposal sources only.  The runtime still closes steps,
    verifies obligations and owns delivery.
    """

    planner: PlanProvider
    actor: StepActor
    plan_reviewer: PlanReviewer | None = None
    step_reviewer: StepReviewer | None = None
    max_step_calls: int = 6
    max_step_reviews: int = 1
    finish_text: str = "completed"
    resumed: bool = False
    _submitted: bool = False
    _step_id: int | None = None
    _calls: int = 0
    _step_reviews: int = 0
    _notices: list[dict[str, Any]] = field(default_factory=list)

    def propose(self, view: ModelView) -> Action:
        if not self._submitted:
            self._submitted = True
            if self.resumed:
                return self._propose_step(view)
            plan = self.planner(view)
            return SubmitPlan(self._review_plan(view, plan))
        return self._propose_step(view)

    def _review_plan(self, view: ModelView, plan: PlanProposal) -> PlanProposal:
        if self.plan_reviewer is None:
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
        added = len(revised.steps) - len(plan.steps)
        self._notices.append(
            {
                "kind": "plan",
                "decision": "amended",
                "added_steps": added,
            }
        )
        return revised

    def _propose_step(self, view: ModelView) -> Action:
        active = view.active_step
        if active is None:
            return Finish(self.finish_text)
        if self._step_id != active.id:
            self._step_id = active.id
            self._calls = 0
            self._step_reviews = 0
        if self._calls >= self.max_step_calls:
            # Bounded exploration: stop spending calls on this step.  Releasing
            # closes it deterministically, so an unmet expectation becomes debt
            # instead of a delivery.
            self._notices.append(
                {
                    "kind": "step",
                    "decision": "budget_exhausted",
                    "step": active.id,
                    "calls": self._calls,
                }
            )
            return ReleaseStep(f"step {active.id}: step budget exhausted")
        # Every iteration costs budget, including notes and invalid proposals,
        # so a confused actor cannot spin forever.
        self._calls += 1
        action = self.actor(view, active, self._calls)
        if isinstance(action, (Think, AskUser, Abort)):
            # Those are legitimate non-work proposals and stay as-is; the step
            # budget still bounds how long they can repeat.
            return action
        if isinstance(action, CallTool):
            return action
        # The actor believes the step is done.  Review it here, before the
        # runtime closes and advances, so a gap is fixed in this step.
        reviewed = self._review_step(view, active, action)
        if isinstance(reviewed, CallTool):
            return reviewed
        self._notices.append(
            {
                "kind": "step",
                "decision": "step_complete",
                "step": active.id,
                "calls": self._calls,
            }
        )
        return ReleaseStep(f"step {active.id}: sub-loop complete")

    def _review_step(
        self, view: ModelView, step: StepContract, action: Action
    ) -> Action | None:
        if self.step_reviewer is None:
            return None
        if self._step_reviews >= self.max_step_reviews:
            return None
        self._step_reviews += 1
        proposal = self.step_reviewer(view, step, action)
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

    def drain_notices(self) -> tuple[dict[str, Any], ...]:
        notices = tuple(self._notices)
        self._notices.clear()
        return notices


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
    "ActFirst",
    "NoToolStrategy",
    "PlanFirst",
    "PlanReact",
    "ReflectFirst",
    "RepairFirst",
]
