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
from pathlib import Path
from typing import Any, Callable

from xenon.kernel.contracts import (
    Abort,
    Action,
    AskUser,
    CallTool,
    Capability,
    Claim,
    Debt,
    Finish,
    ModelView,
    PlanProposal,
    ReleaseStep,
    RequestEscalation,
    StateRead,
    StepContract,
    SubmitPlan,
    Think,
    ToolIntent,
    has_written_payload,
    written_paths,
)

PlanProvider = Callable[[ModelView], PlanProposal]
ActionProvider = Callable[[ModelView], Action]
RepairProvider = Callable[[ModelView, tuple[Debt, ...]], Action]
StepActor = Callable[[ModelView, Any, int], Action]
PlanReviewer = Callable[[ModelView, PlanProposal], PlanProposal | None]
ReflectionProvider = Callable[[ModelView, Finish], Action | None]
StepReviewer = Callable[[ModelView, Any, Action], Action | None]


def _replayable(view: ModelView, step: StepContract, *, approved: bool) -> bool:
    """Whether a follow-plan step can be executed as the plan bound it.

    An **approved** plan was authored by a phase that ran no tools at all, so
    it could not have read anything.  Two shapes are therefore not executable
    as written:

    * an edit intent with *no* content (no ``files``/``edits``) — that is a
      demoted description of work, and only the actor can perform it;
    * an edit intent whose payload *overwrites an existing file* — its
      content is a guess.  Measured C10 approval run: the plan bound
      ``apply_patch`` with ``"<minimal fix of the buggy function body ...>"`
      as the content of a real source file, and the replay destroyed the file
      before the verify step ran.

    New paths keep their payload: authoring content that does not exist needs
    no observation.  Both exceptions go to the step actor, which reads what
    the plan could not and materializes the change the plan described.

    A non-approved follow profile is an in-run mode whose planner output is
    the mode's own contract; its payloads are followed exactly as before.
    """

    intent = step.intent
    if intent is None or intent.capability is not Capability.EDIT:
        return True
    if not approved:
        return True
    if not has_written_payload(intent):
        return False
    workspace = Path(str(getattr(view.task, "workspace", "") or "") or ".")
    for raw in written_paths(intent):
        path = Path(raw)
        if not path.is_absolute():
            path = workspace / path
        if path.exists():
            return False
    return True


@dataclass(frozen=True, slots=True)
class LoopProfile:
    """Which phases of the single loop a mode enables.

    Attributes:
        plan: Propose a plan before acting.
        plan_review: Review plan coverage before the plan is submitted.
        step_scope: Run the plan one step at a time with a per-step sub-loop.
        follow_plan: Execute the plan's own intents instead of asking the actor
            what to do next (deterministic execution, no per-step model call).
            A bound write over an *existing* file is the exception: the plan
            phase never observed that file, so its payload is a guess and the
            step is materialized by the step actor instead.
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
    #: The actor writes its own plan through the ``make_plan`` tool instead of
    #: a separate planner turn.  One identity for the whole run means one
    #: system prompt, which is what keeps the provider's cached prefix intact
    #: across a plan round.  A dedicated planner is a second persona and
    #: therefore a hard cache break at every switch.
    actor_plans: bool = False

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


#: The default coding loop: plan, execute one step at a time, check the step's
#: facts, then let the next step (or the next plan round) close the gaps.
#: Modes are internal presets now; a user does not have to choose a paradigm.
DEFAULT_PROFILE = LoopProfile(
    plan=True,
    step_scope=True,
    step_calls=8,
    step_reflect=True,
)


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
    #: Sticky record of *why* this run started: ``resumed`` means an approved
    #: plan is being executed.  ``reset_for_round`` clears ``resumed`` so the
    #: strategy will accept a new plan, but the plan that this run approved was
    #: still authored without observations, so the stricter follow check must
    #: stay in force for every round.
    _approved_plan: bool = False
    _submitted: bool = False
    _finished: bool = False
    _step_id: int | None = None
    _step_calls: int = 0
    _step_reviews: int = 0
    #: The follow-plan step whose bound intent has already run.  A follow-plan
    #: step has exactly one action; issuing it twice is byte-identical.
    _followed_step: int | None = None
    _followed_intent: ToolIntent | None = None
    _round: int = 0
    _round_calls: int = 0
    _acted: bool = False
    _stopped: bool = False
    _seen: set[str] = field(default_factory=set)
    _notices: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._approved_plan = bool(self.resumed)
        # A plan profile needs *a* way to produce a plan, not necessarily a
        # separate planner role: with `actor_plans` the actor calls the
        # make_plan tool and stays the same identity for the whole run.
        if (
            self.profile.plan
            and self.planner is None
            and not self.resumed
            and not self.profile.actor_plans
        ):
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

    def reset_for_round(self) -> None:
        """Forget round-scoped proposal state so the next plan can be submitted.

        The runtime owns the round decision; the strategy only has to become
        willing to propose a new plan again.  Durable facts stay in the ledger.
        """

        self.resumed = False
        self._submitted = False
        self._finished = False
        self._step_id = None
        self._step_calls = 0
        self._step_reviews = 0
        self._followed_step = None
        self._followed_intent = None
        self._round = 0
        self._round_calls = 0
        self._acted = False
        self._stopped = False
        self._seen.clear()
        self._notices.clear()

    def propose(self, view: ModelView) -> Action:
        if (
            self.profile.plan
            and not self._submitted
            and not self.resumed
            and self.planner is not None
        ):
            self._submitted = True
            # Submit even an empty plan: the ledger has to record that the
            # planner said "no tool work", otherwise the runtime cannot tell a
            # conversation from a run that did nothing.
            return SubmitPlan(self._review_plan(view, self.planner(view)))
        if self.profile.step_scope and view.active_step is not None:
            return self._step_turn(view)
        if (
            self.profile.plan
            and not self.profile.actor_plans
            and view.active_step is None
            and not view.steps
        ):
            # No plan and no steps: the task needed no tool, so the answer is
            # the whole deliverable.  Asking the finisher once is what turns
            # "empty plan" into an answer instead of an empty turn.  When it
            # already answered (the empty-plan path above), this is a no-op.
            #
            # This branch is for the *separate-planner* profile, where an empty
            # plan is the planner's verdict.  With `actor_plans` there is no
            # planner to have said that: `view.steps` is empty simply because
            # the actor has not planned yet, and taking this branch answered the
            # task before a single tool ran.
            if self.finisher is not None and not self._finished:
                self._finished = True
                return self.finisher(view)
            return Finish(self.finish_text)
        return self._reflect_or_finish(view, self._task_action(view))

    def _task_action(self, view: ModelView) -> Action:
        if self.profile.plan and view.steps and self.finisher and not self._finished:
            self._finished = True
            return self.finisher(view)
        action = self.actor(view)
        self._note_unrun_calls(action)
        return action

    def _note_unrun_calls(self, action: Action) -> None:
        """Tell the model about tool calls its own reply made that did not run.

        A provider is free to answer one turn with several ``tool_use`` blocks
        (Anthropic's ``{"type": "any"}`` means "at least one"), while this loop
        executes exactly one action per turn.  The actor keeps the extra ones
        it had to drop; naming them here is what stops the model from silently
        losing half of what it asked for and then re-requesting it from prose
        instead of from a receipt.

        The note is a hint, not a constraint: it never executes anything and
        never satisfies an obligation.
        """

        skipped = tuple(getattr(self.actor, "skipped_calls", ()) or ())
        answered = tuple(getattr(self.actor, "answered_alongside", ()) or ())
        if isinstance(action, Finish):
            output = None
        else:
            output = {"kind": "parallel", "ran": action.kind.value}
            if skipped:
                output["skipped"] = list(skipped)
            if answered:
                output["answered"] = list(answered)
        if output is None or len(output) == 2:
            return
        self._notices.append(output)

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
        if self.profile.follow_plan and _replayable(
            view, active, approved=self._approved_plan
        ):
            return self._follow_plan(active)
        if self.step_actor is None:
            # A follow-plan profile normally has no actor: the plan is the
            # whole instruction.  It can still arrive with a write the plan
            # phase could not have grounded, and replaying that would destroy
            # the file, so the step is released rather than executed blind.
            return ReleaseStep(
                f"step {active.id}: bound write has no grounded payload; "
                "the plan phase could not have read the file"
            )
        if self.profile.follow_plan and self._step_calls == 0:
            # Plan-guided execution: the step's work stays the plan's, but the
            # actor supplies what the plan could not know.  Recorded once per
            # step so the ledger shows why this step cost a model call while
            # its neighbours did not.
            self._notices.append(
                {
                    "kind": "step",
                    "decision": "materialize_bound_write",
                    "step": active.id,
                }
            )
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
        # ``StateRead`` belongs here: asking the harness a question is a
        # legitimate move inside a step, and the runtime answers it and keeps
        # the step open.  Dropping it released the step instead -- a measured
        # run asked ``get_state what=session``, the answer was rendered and
        # thrown away, and the task finished saying it had no history.
        #
        # ``RequestEscalation`` belongs here for the same reason: a step whose
        # bound call hit the boundary must be able to ask the human for that
        # one call.  Swallowing it released the step, so the ask never reached
        # the approval panel and a read-only count answered a number it could
        # not verify ("这里没有弹出授权").
        if isinstance(
            action,
            (Think, AskUser, Abort, CallTool, StateRead, RequestEscalation),
        ):
            return action
        return self._release_step(view, active)

    def _follow_plan(self, step: StepContract) -> Action:
        if step.intent is None:
            # A follow-plan step is executed by its bound capability.  Without
            # one there is nothing to run, and emitting a thought would burn
            # the repair budget on a step that can never make progress.
            return ReleaseStep(
                f"step {step.id} has no bound capability; "
                "a follow-plan profile cannot act on it"
            )
        intent = step.intent
        if step.prefer and intent.prefer is None:
            intent = replace(intent, prefer=step.prefer)
        # A follow-plan step *is* one bound action.  Repeating it is a
        # byte-identical call that cannot teach the loop anything, so an
        # *exploratory* step (no expectations declared) runs once and is
        # released.  A measured approved plan bound ``read`` to a directory:
        # the receipt was failed, the step declared no expectation, so no
        # repair budget could ever be spent, and the identical call was
        # re-issued until the stuck detector blocked the whole task — the
        # write step behind it never ran.  Steps that *do* declare
        # expectations keep the repair budget: their retries are bounded by
        # the runtime, not by this guard.
        if (
            not step.expects
            and self._followed_step == step.id
            and self._followed_intent == intent
        ):
            return ReleaseStep(
                f"step {step.id}: bound action already executed"
            )
        self._followed_step = step.id
        self._followed_intent = intent
        return CallTool(intent)

    def _release_step(self, view: ModelView, step: StepContract) -> Action:
        """Review the step before it is released, then release it."""
        reviewed = self._review_step(view, step)
        # An escalation request is a *call the reviewer wants run*, so it has
        # to leave the step sub-loop exactly like a tool call.  Only forwarding
        # ``CallTool`` here is how the reviewer's ask was dropped on the floor.
        if isinstance(reviewed, (CallTool, RequestEscalation)):
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
        if isinstance(proposal, RequestEscalation):
            # The step cannot be completed as bound: it needs one call the
            # boundary refused.  Record it as a correction (the reviewer's
            # move) and let the runtime put it to the human.
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
