"""Small, stateless strategies for the single loop (#198).

A strategy only proposes; it never executes tools or mutates runtime state.
Plan/step state (active step, closure) lives in ``LoopRuntime``; a strategy may
hold only its own proposal cursor.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable

from xenon.kernel.contracts import (
    Action,
    CallTool,
    Claim,
    Debt,
    Finish,
    ModelView,
    PlanProposal,
    SubmitPlan,
    Think,
)

PlanProvider = Callable[[ModelView], PlanProposal]
ActionProvider = Callable[[ModelView], Action]
RepairProvider = Callable[[ModelView, tuple[Debt, ...]], Action]
ReflectionProvider = Callable[[ModelView], Action | None]


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
            return proposal
        return action


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
    "ReflectFirst",
    "RepairFirst",
]
