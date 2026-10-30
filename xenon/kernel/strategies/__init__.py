"""Small, stateless strategies for the single loop (#198).

A strategy only proposes; it never executes tools or mutates runtime state.
Plan/step state (active step, closure) lives in ``LoopRuntime``; a strategy may
hold only its own proposal cursor.
"""

from __future__ import annotations

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
    SubmitPlan,
    Think,
)

PlanProvider = Callable[[ModelView], PlanProposal]
ActionProvider = Callable[[ModelView], Action]
RepairProvider = Callable[[ModelView, tuple[Debt, ...]], Action]
ReflectionProvider = Callable[[ModelView], Action | None]
StepActor = Callable[[ModelView, Any, int], Action]


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
    """Act, then let a bounded reflector propose follow-up work.

    A reflector is a proposal source, not a judge.  It can only add one more
    tool call; it can never mark a task complete, close debt, or turn an
    unverified claim into evidence.  Completion stays with the delegate's
    ``Finish`` and the controller's deterministic verification.
    """

    base: Any  # Strategy protocol; Any avoids a circular import
    reflector: ReflectionProvider
    max_reflections: int = 1
    _reflections: int = 0
    _acted: bool = False
    _notices: list[dict[str, Any]] = field(default_factory=list)

    def propose(self, view: ModelView) -> Action:
        action = self.base.propose(view)
        if isinstance(action, CallTool):
            self._acted = True
            return action
        if not isinstance(action, Finish):
            return action
        if not self._acted or self._reflections >= self.max_reflections:
            return action
        self._reflections += 1
        proposal = self.reflector(view)
        # Only extra work is accepted.  A reflector that says "done" or asks a
        # question cannot replace the delegate's finish, so it never owns the
        # delivery decision.
        if isinstance(proposal, CallTool):
            self._acted = True
            self._notices.append(
                {
                    "kind": "reflection",
                    "decision": "correction",
                    "capability": proposal.intent.capability.value,
                    "round": self._reflections,
                }
            )
            return proposal
        self._notices.append(
            {
                "kind": "reflection",
                "decision": "declined",
                "capability": (
                    proposal.intent.capability.value
                    if isinstance(proposal, CallTool)
                    else ""
                ),
                "round": self._reflections,
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


@dataclass(slots=True)
class PlanReact:
    """Plan globally, then run a bounded sub-loop per step.

    Unlike ``PlanFirst``, the actor sees one step at a time and gets a small
    per-step call budget.  A step is closed by the runtime's deterministic
    verification; this strategy only decides when to stop exploring it.  It
    never executes a step ahead of the active one and never declares success.
    """

    planner: PlanProvider
    actor: StepActor
    max_step_calls: int = 6
    finish_text: str = "completed"
    resumed: bool = False
    _submitted: bool = False
    _step_id: int | None = None
    _calls: int = 0
    _notices: list[dict[str, Any]] = field(default_factory=list)

    def propose(self, view: ModelView) -> Action:
        if not self._submitted:
            self._submitted = True
            if not self.resumed:
                return SubmitPlan(self.planner(view))
        active = view.active_step
        if active is None:
            return Finish(self.finish_text)
        if self._step_id != active.id:
            self._step_id = active.id
            self._calls = 0
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
        if isinstance(action, CallTool):
            return action
        if isinstance(action, (Think, AskUser, Abort)):
            # Those are legitimate non-work proposals and stay as-is; the step
            # budget still bounds how long they can repeat.
            return action
        # Anything else means "this step is done from the actor's view".  The
        # runtime still owns closing and verifying it.
        self._notices.append(
            {
                "kind": "step",
                "decision": "step_complete",
                "step": active.id,
                "calls": self._calls,
            }
        )
        return ReleaseStep(f"step {active.id}: sub-loop complete")

    def drain_notices(self) -> tuple[dict[str, Any], ...]:
        notices = tuple(self._notices)
        self._notices.clear()
        return notices


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
