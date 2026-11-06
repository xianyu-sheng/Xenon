"""Single-loop runtime (#196).

The runtime is the only writer.  Strategies propose actions; policies veto;
the gateway executes; the controller verifies; the ledger records.  There is no
second state machine and no direct tool dispatch in this module.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import replace
from typing import Any, Mapping, Sequence

from xenon.kernel.audit import AuditCoordinator
from xenon.kernel.cancel import ModelCancelled
from xenon.kernel.contracts import (
    Abort,
    Action,
    AskUser,
    AuditProposal,
    BindingStatus,
    CallTool,
    Claim,
    ControlDecisionKind,
    Debt,
    EffectKind,
    EvidenceStatus,
    Finish,
    FsDelete,
    FsWrite,
    Obligation,
    ObligationSource,
    PlanProposal,
    PolicyDecision,
    PolicyDecisionKind,
    PropositionKind,
    ReceiptStatus,
    ReleaseStep,
    RequestEscalation,
    RequestVerification,
    Resolution,
    ResolutionStatus,
    StepContract,
    StepKind,
    SubmitPlan,
    TaskMode,
    TaskPhase,
    TaskResult,
    TaskSpec,
    Think,
    ToolReceipt,
    UpdatePlan,
    VcsCommit,
    Verdict,
    VerdictStatus,
    VerificationStage,
)
from xenon.kernel.controller import index_facts
from xenon.kernel.finalize import FinalizeDecision, FinalizeGuard
from xenon.kernel.loop_signals import DEFAULT_MAX_IDENTICAL, StuckDetector
from xenon.kernel.phases import assert_transition
from xenon.kernel.protocols import (
    ApprovalProvider,
    AuditProposalProvider,
    EventSink,
    Finalizer,
    Ledger,
    Policy,
    Strategy,
    ToolCatalog,
    ToolGateway,
    VerificationController,
)
from xenon.kernel.steering import SteeringState, parse_steering
from xenon.kernel.usage import UsageTotals, usage_payload
from xenon.kernel.view import build_model_view
from xenon.ledger.events import (
    EventKind,
    EventSource,
    canonical_json,
    receipt_event_payload,
    receipt_from_payload,
)

logger = logging.getLogger(__name__)


def _max_identical_calls(spec: TaskSpec) -> int:
    """Identical-call budget: explicit spec wins, else the default."""

    override = getattr(spec, "max_identical_calls", 0)
    try:
        value = int(override or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else DEFAULT_MAX_IDENTICAL


#: "Do not execute this action; let the model propose again."  Returning
#: ``None`` from a loop-signal handler means "the action is still fine", which
#: is not what a forced finish means (#407).
_SKIP_ACTION = object()


_POLICY_REFUSED = object()
_MAX_POLICY_REFUSALS = 3
"""Policy said no to *this action*; the loop must continue without it.

``None`` still means "allowed" (the historical contract), so a refusal needs a
distinct value.  Returning ``None`` here would fall through to dispatch and run
the very action the policy denied.
"""


class _RuntimeServices:
    """Small gateway-facing surface; never exposes the mutable ledger."""

    def __init__(self, runtime: "LoopRuntime") -> None:
        self._runtime = runtime

    @property
    def task_id(self) -> str:
        return self._runtime._spec.task_id if self._runtime._spec else ""

    @property
    def workspace(self) -> str:
        return self._runtime._spec.workspace if self._runtime._spec else ""

    @property
    def current_step_id(self) -> str | None:
        return self._runtime._current_step_id()

    @property
    def verified_paths(self) -> tuple[str, ...]:
        return self._runtime._verified_paths()

    def is_cancelled(self) -> bool:
        return self._runtime._cancelled


def _payload_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _ledger_phase(events: Sequence[Any]) -> TaskPhase:
    phase = TaskPhase.CREATED
    for event in events:
        if event.kind is EventKind.STATE_TRANSITION:
            target = str((event.payload or {}).get("to") or "")
            try:
                phase = TaskPhase(target)
            except ValueError:
                continue
    return phase


def _plan_text(plan: Any) -> str:
    """Render an accepted plan without claiming execution evidence."""

    analysis = str(getattr(plan, "analysis", "") or "").strip()
    lines: list[str] = [analysis] if analysis else []
    for step in tuple(getattr(plan, "steps", ()) or ()):
        kind = getattr(step.kind, "value", str(step.kind))
        targets = ", ".join(
            str(item) for item in getattr(step, "targets", ()) or ()
        )
        suffix = f" · {targets}" if targets else ""
        lines.append(f"{step.id}. {kind}{suffix}")
    return chr(10).join(lines) or "计划已提交，等待确认。"


class LoopRuntime:
    """One loop, one writer, explicit phase machine."""

    def __init__(
        self,
        *,
        catalog: ToolCatalog,
        gateway: ToolGateway,
        controller: VerificationController,
        ledger: Ledger,
        sink: EventSink | None = None,
        policies: Sequence[Policy] = (),
        finalize: FinalizeGuard | None = None,
        finalizer: Finalizer | None = None,
        approval: ApprovalProvider | None = None,
        audit: AuditCoordinator | None = None,
        audit_provider: AuditProposalProvider | None = None,
        worktree_probe: Any = None,
    ) -> None:
        self._catalog = catalog
        self._gateway = gateway
        self._controller = controller
        self._ledger = ledger
        self._readonly = _readonly_view(ledger)
        self._sink = sink
        self._policies = tuple(policies)
        self._finalize = finalize or FinalizeGuard()
        self._finalizer = finalizer
        self._approval = approval
        self._audit = audit or AuditCoordinator()
        self._audit_provider = audit_provider
        # Injected by the host: the kernel must not shell out to git.
        self._worktree_probe = worktree_probe
        self._probed_signature = ""
        self._audit_rounds = 0
        self._approved_grants: list[Any] = []
        self.last_grant: Any = None
        self._services = _RuntimeServices(self)

        self._spec: TaskSpec | None = None
        self._phase = TaskPhase.CREATED
        self._steps = 0
        self._tool_calls = 0
        self._cancelled = False
        self._deadline: float | None = None
        self._plan: Any = None
        self._plan_steps: tuple[StepContract, ...] = ()
        self._closed_steps: set[int] = set()
        self._active_step: StepContract | None = None
        self._plan_since_seq = 0
        self._verdicts: list[Verdict] = []
        self._debt: list[Any] = []
        self._step_verdicts: dict[int, list[Verdict]] = {}
        self._repair_attempts: dict[str, int] = {}
        self._replans = 0
        #: Plan rounds started in this turn (1-based).  A task-level audit with
        #: gaps starts the next round until ``max_plan_rounds``.
        self._plan_rounds = 0
        #: The last Finish answer, so a turn that never needed a tool can be
        #: told apart from a turn that did nothing.
        self._last_answer = ""
        self._stuck = StuckDetector()
        self._clock: Any = time.time
        self._last_progress_at = self._clock()
        self._usage = UsageTotals()
        self._steering = SteeringState()
        self._loop_taught: dict[str, int] = {}
        self._loop_notes: list[str] = []
        # Set once a loop is stopped: the run must answer with what it has
        # instead of returning "no delivery" after doing real work (#407).
        self._forced_finish = False
        self._forced_finish_allowance = 0
        self._ok_receipts = 0
        #: Refusals that happened *before* dispatch (#417).  A high count is a
        #: protocol problem, not a model problem: the agent keeps proposing
        #: calls the catalog cannot bind.
        self._refusals: dict[str, int] = {}
        #: (step ordinal, obligation) for every edit this turn performed, so a
        #: plan-less turn still has a V1 to close over (#420).
        self._edit_steps: list[tuple[int, Obligation]] = []
        self._accept_debt = False
        self._context_digest = ""
        self._steering_source: Any = None
        self._compaction_signature = ""
        self._finalize_decision: FinalizeDecision | None = None
        self._run_start_seq = 0

    # ── public surface ─────────────────────────────────────────────────

    def request_cancel(self) -> None:
        self._cancelled = True

    def _boundary_note(self) -> str:
        """The execution boundary the policies actually enforce (#165)."""
        for policy in self._policies:
            describe = getattr(policy, "describe_boundary", None)
            if callable(describe):
                try:
                    return str(describe())
                except Exception:  # noqa: BLE001 — a note must not fail a run
                    return ""
        return ""

    def status_snapshot(self) -> dict[str, Any]:
        """#372 E1: the live kernel state, all of it from runtime facts.

        The REPL renders this verbatim — it never guesses phase, steps or
        stall from its own counters.  Stalled seconds are "no new information
        since" wall-clock, which is the loop detector's own definition.
        """

        now = float(self._clock())
        stalled = 0.0
        if self._last_progress_at:
            stalled = max(0.0, now - float(self._last_progress_at))
        return {
            "phase": self._phase.value if self._phase else "",
            "steps": int(self._steps or 0),
            "tokens": int(getattr(self._usage, "total", 0) or 0),
            # Context depth for the last request; a cumulative total cannot
            # tell an operator whether the window is filling up (#408).
            "context_tokens": int(getattr(self._usage, "last_prompt", 0) or 0),
            "stalled_seconds": stalled,
            "tool_calls": int(self._tool_calls or 0),
            "refusals": dict(self._refusals),
            "refusal_total": sum(self._refusals.values()),
        }

    def run(self, spec: TaskSpec, strategy: Strategy) -> TaskResult:
        ledger_task_id = getattr(self._ledger, "task_id", "")
        if ledger_task_id and ledger_task_id != spec.task_id:
            raise ValueError(
                f"ledger task_id {ledger_task_id!r} does not match {spec.task_id!r}"
            )
        self._spec = spec
        self._phase = TaskPhase.CREATED
        self._steps = 0
        self._tool_calls = 0
        self._verdicts = []
        self._debt = []
        self._step_verdicts: dict[int, list[Verdict]] = {}
        self._repair_attempts = {}
        self._replans = 0
        self._stuck = StuckDetector(max_identical=_max_identical_calls(spec))
        self._last_progress_at = self._clock()
        self._usage = UsageTotals()
        self._steering = SteeringState()
        self._loop_taught = {}
        self._loop_notes = []
        self._forced_finish = False
        self._forced_finish_allowance = 0
        self._ok_receipts = 0
        #: Refusals that happened *before* dispatch (#417).  A high count is a
        #: protocol problem, not a model problem: the agent keeps proposing
        #: calls the catalog cannot bind.
        self._refusals: dict[str, int] = {}
        self._accept_debt = bool(spec.accept_debt)
        self._compaction_signature = ""
        self._finalize_decision = None
        self._run_start_seq = 0
        self._audit_rounds = 0
        self._revoke_grants()
        self._plan = None
        self._plan_steps = ()
        self._closed_steps = set()
        self._active_step = None
        self._plan_since_seq = 0
        self._deadline = self._compute_deadline(spec)

        self._append(
            EventKind.TASK_STARTED,
            EventSource.RUNTIME,
            {
                "objective": spec.objective,
                "user_objective": spec.user_objective,
                "workspace": spec.workspace,
                "mode": spec.mode.value,
                "constraints": list(spec.constraints),
                # Empty for a top-level task; the call tree is derived from here.
                "parent_task_id": spec.parent_task_id,
                "batch_id": spec.batch_id,
            },
        )
        self._run_start_seq = self._ledger.watermark()
        # #420: V2 had nothing to verify because nothing ever filled
        # `TaskSpec.obligations`.  Derive them from the task text here, in the
        # harness layer, and record them: coverage that is not derived is
        # coverage that cannot exist.
        if not spec.obligations:
            from xenon.harness.intake import derive_obligations

            derived = derive_obligations(
                spec.user_objective or spec.objective,
                since_seq=self._run_start_seq,
            )
            if derived:
                spec = replace(spec, obligations=derived)
                self._spec = spec
        self._transition(TaskPhase.INTAKE, "task accepted")
        self._append(
            EventKind.INTAKE_RECORDED,
            EventSource.RUNTIME,
            {
                "mode": spec.mode.value,
                "constraints": list(spec.constraints),
                "obligations": [
                    {"id": item.id, "kind": item.kind.value, "target": item.target}
                    for item in spec.obligations
                ],
            },
        )
        self._transition(TaskPhase.RUNNING, "intake complete")

        return self._run_loop(spec, strategy)

    def resume_planned(
        self,
        spec: TaskSpec,
        strategy: Strategy,
        plan: PlanProposal,
        *,
        plan_digest: str,
        approval_id: str,
    ) -> TaskResult:
        """Resume one approved plan-only task in the same ledger.

        The plan-only run already wrote ``plan_accepted`` and stopped at
        ``PLANNED``.  This method appends the external approval fact, moves the
        same task back to ``RUNNING`` and executes the accepted steps.  It does
        not append a second ``task_started`` and never asks the strategy to
        submit a new plan.

        ``approval_id`` is required so the resumed execution is attributable.
        When ``plan_digest`` is supplied it must match the accepted plan in the
        ledger; a mismatch fails closed.
        """

        if not approval_id:
            raise ValueError("resume_planned requires an approval_id")
        if not plan_digest:
            raise ValueError("resume_planned requires a plan_digest")
        if spec.mode is TaskMode.PLAN:
            raise ValueError("resume_planned requires an execution TaskMode")
        ledger_task_id = getattr(self._ledger, "task_id", "")
        if ledger_task_id and ledger_task_id != spec.task_id:
            raise ValueError(
                f"ledger task_id {ledger_task_id!r} does not match {spec.task_id!r}"
            )
        events = self._ledger.read()
        accepted = [
            event for event in events if event.kind is EventKind.PLAN_ACCEPTED
        ]
        if not accepted:
            raise ValueError("ledger has no accepted plan to resume")
        accepted_event = accepted[-1]
        raw_plan = accepted_event.payload.get("plan")
        if not isinstance(raw_plan, Mapping):
            raise ValueError("accepted plan payload is not an object")
        stored_digest = _payload_digest(raw_plan)
        if plan_digest != stored_digest:
            raise ValueError("approved plan digest does not match the ledger")
        if _ledger_phase(events) is not TaskPhase.PLANNED:
            raise ValueError("ledger is not in the planned phase")
        if any(
            event.kind in {EventKind.STEP_STARTED, EventKind.TOOL_RECEIPT}
            and event.seq > accepted_event.seq
            for event in events
        ):
            raise ValueError("planned ledger already has execution events")
        if not plan.steps:
            raise ValueError("approved plan has no steps")

        self._spec = spec
        self._phase = TaskPhase.PLANNED
        self._steps = 0
        self._tool_calls = 0
        self._verdicts = []
        self._debt = []
        self._repair_attempts = {}
        self._replans = 0
        self._finalize_decision = None
        self._audit_rounds = 0
        self._revoke_grants()
        self._plan = plan
        self._plan_since_seq = accepted_event.seq
        self._plan_steps = tuple(
            self._baseline_step(step) for step in plan.steps
        )
        self._closed_steps = set()
        self._deadline = self._compute_deadline(spec)
        self._active_step = self._select_ready_step()
        if self._active_step is None:
            raise ValueError("approved plan has no ready step")

        self._append(
            EventKind.PLAN_APPROVED,
            EventSource.USER,
            {
                "approved": True,
                "approval_id": approval_id,
                "plan_digest": stored_digest,
            },
        )
        self._transition(TaskPhase.RUNNING, "approved plan resumed")
        self._append(
            EventKind.STEP_STARTED,
            EventSource.RUNTIME,
            self._step_payload(self._active_step),
            step_id=self._current_step_id(),
        )
        self._run_start_seq = self._ledger.watermark()
        return self._run_loop(spec, strategy)

    def _run_loop(self, spec: TaskSpec, strategy: Strategy) -> TaskResult:
        while True:
            limited = self._check_limits()
            if limited is not None:
                return limited

            self._consume_steering()
            # A step verdict may ask "are there stray changes?", so the
            # observation has to exist before obligations are reconciled —
            # not only on the Finish path.
            self._probe_worktree()
            self._note_session_context()
            view = build_model_view(
                spec=spec,
                catalog=self._catalog,
                ledger=self._readonly,
                debt=tuple(self._debt),
                budget=spec.budget,
                phase=self._phase,
                active_step=self._active_step,
                steps=self._plan_steps,
                step_verdicts=self._active_step_verdicts(),
                tokens_used=self._usage.total,
                extra_constraints=self._steering.extra_constraints(),
                superseded=self._steering.superseded(),
                strategy_hint=self._steering.strategy_hint(),
                notes=tuple(self._loop_notes),
                boundary=self._boundary_note(),
            )
            self._transition(TaskPhase.THINKING, "propose action")
            try:
                action = strategy.propose(view)
            except ModelCancelled:
                # The operator stopped us.  Not a failure and not a budget
                # block: the ledger must keep the three apart.
                self._transition(TaskPhase.ABORTED, "model call cancelled")
                return self._result(
                    accepted=False, evidence=EvidenceStatus.UNVERIFIED
                )
            except Exception:
                self._transition(TaskPhase.FAILED, "strategy error")
                raise
            self._transition(TaskPhase.RUNNING, "action proposed")
            self._steps += 1
            self._record_strategy_notices(strategy, action)

            # A plan-only task must never execute a tool even if its strategy
            # misbehaves.  The accepted plan is the whole result.
            if self._plan_only() and not isinstance(action, SubmitPlan):
                self._transition(
                    TaskPhase.BLOCKED,
                    "plan-only task requires a plan proposal",
                )
                return self._result(
                    accepted=False, evidence=EvidenceStatus.UNVERIFIED
                )

            veto = self._check_policies(view, action)
            if veto is _POLICY_REFUSED:
                continue
            if veto is not None:
                return veto

            allowed_once = False
            if (
                self._forced_finish
                and self._forced_finish_allowance > 0
                and isinstance(action, CallTool)
            ):
                self._forced_finish_allowance -= 1
                allowed_once = True
                self._append(
                    EventKind.CONTROL_DECISION,
                    EventSource.GATE,
                    {
                        "decision": "force_finish_allow_once",
                        "reason": "the wrap-up needs one more call",
                        "scope": [action.intent.capability.value],
                        "remaining": self._forced_finish_allowance,
                    },
                    step_id=self._current_step_id(),
                )
            if (
                self._forced_finish
                and self._forced_finish_allowance <= 0
                and not allowed_once
                and isinstance(action, CallTool)
            ):
                # The forced-finish note is an instruction, not a suggestion:
                # a loop that keeps proposing tools is how a turn dies with its
                # evidence unread (#407).
                reason = (
                    "tools are closed after a detected loop; answer with the "
                    f"{self._ok_receipts} receipt(s) already recorded"
                )
                self._append(
                    EventKind.POLICY_DECISION,
                    EventSource.GATE,
                    {
                        "kind": PolicyDecisionKind.VETO.value,
                        "reason": reason,
                        "scope": ["loop", "forced_finish"],
                    },
                    step_id=self._current_step_id(),
                )
                return self._budget_block(reason, ("loop", "forced_finish"))

            if isinstance(action, Think):
                self._append(
                    EventKind.THOUGHT,
                    EventSource.LLM,
                    {"text": action.text},
                )
                if (
                    self._active_step is not None
                    and self._active_step.intent is None
                ):
                    closed = self._close_active_step()
                    exhausted = self._record_attempts(closed)
                    self._sync_debt(closed)
                    if exhausted:
                        self._release_step_with_gaps(
                            exhausted, "repair budget exhausted"
                        )
                continue

            if isinstance(action, SubmitPlan):
                blocked = self._accept_plan(action.plan)
                if blocked is not None:
                    return blocked
                continue

            if isinstance(action, UpdatePlan):
                try:
                    self._apply_plan(
                        action.plan,
                        updated=True,
                        start_active=not self._plan_only(),
                    )
                except ValueError as exc:
                    blocked = self._reject_plan(exc)
                    if blocked is not None:
                        return blocked
                continue

            if isinstance(action, RequestVerification):
                self._append(
                    EventKind.CLAIM_PROPOSED,
                    EventSource.LLM,
                    _claim_payload(self._baseline_claim(action.claim)),
                )
                continue

            if isinstance(action, CallTool):
                blocked = self._call_tool(action)
                if blocked is not None:
                    return blocked
                continue

            if isinstance(action, Finish):
                self._last_answer = action.answer
                result = self._finish(action, strategy)
                if result is not None:
                    return result
                continue

            if isinstance(action, ReleaseStep):
                result = self._release_active_step(action.reason)
                if result is not None:
                    return result
                continue

            if isinstance(action, RequestEscalation):
                blocked = self._request_escalation(view, action)
                if blocked is not None:
                    return blocked
                continue

            if isinstance(action, AskUser):
                self._append(
                    EventKind.USER_QUESTION,
                    EventSource.RUNTIME,
                    {"question": action.question},
                )
                self._transition(TaskPhase.BLOCKED, "user input required")
                return self._result(
                    accepted=False,
                    evidence=EvidenceStatus.ABSTAINED,
                )

            if isinstance(action, Abort):
                self._transition(TaskPhase.ABORTED, action.reason)
                return self._result(
                    accepted=False,
                    evidence=EvidenceStatus.UNVERIFIED,
                )

            self._transition(TaskPhase.FAILED, f"unknown action: {action!r}")
            return self._result(
                accepted=False,
                evidence=EvidenceStatus.UNVERIFIED,
            )

    # ── action handlers ────────────────────────────────────────────────

    def _accept_plan(self, plan: Any) -> TaskResult | None:
        plan_only = self._plan_only()
        if plan_only and not tuple(getattr(plan, "steps", ())):
            reason = str(getattr(plan, "analysis", "") or "")[:300]
            self._append(
                EventKind.BINDING_FAILED,
                EventSource.GATE,
                {
                    "status": "invalid_plan",
                    "candidates": [],
                    "reason": reason or "plan-only requires at least one step",
                },
            )
            self._transition(TaskPhase.BLOCKED, "invalid empty plan")
            return self._result(
                accepted=False,
                evidence=EvidenceStatus.UNVERIFIED,
                answer=reason or "计划生成失败：没有可执行步骤。",
            )
        try:
            self._apply_plan(plan, updated=False, start_active=not plan_only)
        except ValueError as exc:
            if plan_only:
                self._transition(TaskPhase.BLOCKED, "invalid plan")
                return self._result(
                    accepted=False,
                    evidence=EvidenceStatus.UNVERIFIED,
                    answer=f"计划生成失败：{exc}"[:300],
                )
            return self._reject_plan(exc)
        self._plan_rounds += 1
        # Accepting a plan is progress; without this a slow planner could be
        # killed by the stall brake before its first step runs.
        self._last_progress_at = self._clock()
        if plan_only:
            self._transition(
                TaskPhase.PLANNED, "plan accepted; waiting for approval"
            )
            return self._result(
                accepted=False,
                evidence=EvidenceStatus.UNVERIFIED,
                answer=_plan_text(plan),
            )
        return None

    def _plan_only(self) -> bool:
        return self._spec is not None and self._spec.mode is TaskMode.PLAN

    def _reject_plan(self, exc: ValueError) -> TaskResult | None:
        self._replans += 1
        self._append(
            EventKind.BINDING_FAILED,
            EventSource.GATE,
            {
                "status": "invalid_plan",
                "candidates": [],
                "reason": str(exc),
            },
        )
        if self._replans > self._max_replans():
            self._note_replan_cap("invalid plan")
        self._transition(TaskPhase.REPLANNING, "invalid plan")
        self._transition(TaskPhase.RUNNING, "replan after invalid plan")
        return None

    def _note_replan_cap(self, reason: str) -> None:
        """Stop *nagging* about replans; do not end the turn for it.

        The hard stop here used to fire before the plan-round budget, so a
        third round could be killed by an invalid-plan counter rather than by
        the round cap.  The wall clock/stall detector is the real brake.
        """

        self._append(
            EventKind.CONTROL_DECISION,
            EventSource.GATE,
            {
                "decision": "replan_cap_reached",
                "reason": reason,
                "continued": True,
                "replans": self._replans,
            },
        )
        self._loop_notes.append(
            "Planning has failed repeatedly. Submit a much smaller plan "
            "(one target), or finish with what you have; the remaining gaps "
            "will be disclosed rather than hidden."
        )
        del self._loop_notes[:-5]

    def _apply_plan(
        self, plan: Any, *, updated: bool, start_active: bool = True
    ) -> None:
        raw_steps = tuple(getattr(plan, "steps", ()))
        self._validate_steps(raw_steps)
        self._plan_since_seq = self._ledger.watermark()
        steps = tuple(self._baseline_step(step) for step in raw_steps)
        payload = {"plan": _plan_payload(plan, steps=steps)}
        if updated:
            self._append(EventKind.PLAN_UPDATED, EventSource.RUNTIME, payload)
        else:
            self._append(EventKind.PLAN_PROPOSED, EventSource.LLM, payload)
            self._append(EventKind.PLAN_ACCEPTED, EventSource.RUNTIME, payload)
        self._plan = plan
        self._plan_steps = steps
        self._closed_steps.clear()
        if not start_active:
            self._active_step = None
            return
        self._active_step = self._select_ready_step()
        if self._active_step is not None:
            self._append(
                EventKind.STEP_STARTED,
                EventSource.RUNTIME,
                self._step_payload(self._active_step),
                step_id=self._current_step_id(),
            )

    @staticmethod
    def _validate_steps(steps: Sequence[StepContract]) -> None:
        ids = [step.id for step in steps]
        if len(ids) != len(set(ids)):
            raise ValueError("plan steps must have unique ids")
        known = set(ids)
        obligation_ids: list[str] = []
        for step in steps:
            for obligation in step.expects:
                if not obligation.id:
                    raise ValueError(
                        f"step {step.id} has an expectation without an id"
                    )
                obligation_ids.append(obligation.id)
            for dependency in step.depends_on:
                if dependency == step.id:
                    raise ValueError(
                        f"step {step.id} cannot depend on itself"
                    )
                if dependency not in known:
                    raise ValueError(
                        f"step {step.id} depends on unknown step {dependency}"
                    )
        if len(obligation_ids) != len(set(obligation_ids)):
            raise ValueError("plan expectations must have unique ids")
        _assert_acyclic(steps)

    def _baseline_step(self, step: StepContract) -> StepContract:
        expects = tuple(
            self._baseline_obligation(item) for item in step.expects
        )
        if expects == step.expects:
            return step
        return replace(step, expects=expects)

    def _baseline_obligation(self, obligation: Obligation) -> Obligation:
        if obligation.since_seq:
            return obligation
        # Task scope, not round scope.  A fact produced in an earlier plan
        # round still belongs to this task: a later round that re-declares
        # ``file_changed(common.py)`` must see the write round 1 already
        # recorded, instead of reporting "no changed file fact" and pushing the
        # model to edit the same file again (observed on SmartBench #26 with
        # v4-pro: one bound V2 row and two unresolved V1 rows for one write).
        return replace(obligation, since_seq=self._run_start_seq)

    def _call_tool(self, action: CallTool) -> TaskResult | None:
        try:
            return self._call_tool_once(action)
        finally:
            # A one-shot approval must never survive the call, including
            # binding failures and gateway exceptions.
            self._revoke_grants()

    def _note_refusal(self, reason: str, capability: str) -> None:
        """Count a pre-dispatch refusal so it can be reported, not just logged."""

        key = f"{capability}:{reason.split(':')[0][:60]}"
        self._refusals[key] = self._refusals.get(key, 0) + 1

    def _call_tool_once(self, action: CallTool) -> TaskResult | None:
        binding = self._catalog.bind(action.intent)
        if binding.status is not BindingStatus.BOUND or binding.call is None:
            self._note_refusal(
                str(binding.reason or binding.status.value),
                action.intent.capability.value,
            )
            self._replans += 1
            self._append(
                EventKind.BINDING_FAILED,
                EventSource.GATE,
                {
                    "status": binding.status.value,
                    "candidates": list(binding.candidates),
                    "reason": binding.reason,
                },
                step_id=self._current_step_id(),
            )
            if self._replans > self._max_replans():
                self._note_replan_cap("no deterministic binding")
            self._transition(TaskPhase.REPLANNING, "no deterministic binding")
            self._transition(TaskPhase.RUNNING, "replan after binding failure")
            return None

        # Identical repetition is knowable before the call, so it is refused
        # before the call: there is nothing to learn from executing it again.
        repeat = self._stuck.note_call(
            binding.call.spec_name, binding.call.params
        )
        if repeat is not None:
            result = self._on_loop_signal(
                repeat.kind,
                repeat.detail,
                binding.call.spec_name,
                repeat.reason,
                repeat,
            )
            if result is _SKIP_ACTION:
                return None  # do not execute: the model must answer instead
            if result is not None:
                return result

        self._transition(TaskPhase.TOOL_CALL, binding.call.spec_name)
        try:
            receipt = self._gateway.execute(binding.call, self._services)
        except Exception as exc:  # noqa: BLE001 — a loop must not die on a tool
            # No receipt is fabricated: the failure is recorded as a policy
            # veto with its reason, and the run stops unverified.  A silent
            # traceback out of ``run()`` would leave the ledger with a
            # ``tool_requested`` and nothing else.
            message = f"{type(exc).__name__}: {exc}"
            self._transition(TaskPhase.RECONCILING, "tool raised")
            self._append(
                EventKind.POLICY_DECISION,
                EventSource.GATE,
                {
                    "kind": PolicyDecisionKind.VETO.value,
                    "reason": f"tool execution failed: {message}",
                    "scope": [binding.call.spec_name],
                },
                step_id=self._current_step_id(),
            )
            return self._budget_block(
                "tool execution failed", (binding.call.spec_name,)
            )
        self._append(
            EventKind.TOOL_RECEIPT,
            EventSource.TOOL,
            receipt_event_payload(receipt),
            step_id=self._current_step_id(),
        )
        if receipt.status is ReceiptStatus.OK:
            self._ok_receipts += 1
            # #420: the implicit V1 step is derived from what the receipt says
            # it wrote, not from the action's `targets` — an `apply_patch`
            # carries its paths inside the effect, and using the (empty) intent
            # targets produced "unverifiable" verdicts that blocked a delivery
            # which had actually succeeded.
            for effect in receipt.effects:
                if not isinstance(effect, FsWrite) or not effect.changed:
                    continue
                for path in effect.paths:
                    self._edit_steps.append(
                        (
                            len(self._edit_steps) + 1,
                            Obligation(
                                id=f"implicit-step-{len(self._edit_steps) + 1}",
                                kind=PropositionKind.FILE_CHANGED,
                                target=str(path),
                                since_seq=self._run_start_seq,
                            ),
                        )
                    )
        else:
            # #417: a failure is evidence, not a stop.  Put one actionable line
            # in the next view so the model changes something instead of
            # repeating the same call (which is how a turn used to die).
            from xenon.kernel.failure_advice import advice_for_failure

            advice = advice_for_failure(receipt_event_payload(receipt)["receipt"])
            if advice:
                self._loop_notes.append(f"{receipt.tool}: {advice}")
                del self._loop_notes[:-5]
                # #421: an unobservable mechanism is a claim, not a feature.
                # The advice is recorded so an audit can answer "did a failure
                # actually teach the next step?" from the ledger alone.
                self._append(
                    EventKind.CONTROL_DECISION,
                    EventSource.GATE,
                    {
                        "decision": "teach_next_step",
                        "reason": advice,
                        "scope": [str(receipt.tool)],
                    },
                    step_id=self._current_step_id(),
                )
        if receipt.status is ReceiptStatus.DENIED:
            self._append(
                EventKind.POLICY_DECISION,
                EventSource.GATE,
                {
                    "kind": PolicyDecisionKind.VETO.value,
                    "reason": receipt.error or "permission denied",
                    "scope": [receipt.tool],
                },
                step_id=self._current_step_id(),
            )
        self._tool_calls += 1
        self._transition(TaskPhase.RECONCILING, "receipt recorded")

        # Deterministic loop detection.  This runs on receipts only, so a run
        # that keeps making progress is never stopped by it — but a run that is
        # retrying the same wall with different arguments is stopped with a
        # *diagnosis* instead of being left to exhaust the step budget.
        signal = self._stuck.observe(receipt)
        if signal is not None:
            result = self._on_loop_signal(
                signal.kind,
                signal.detail,
                signal.tool or receipt.tool,
                signal.reason,
                signal,
            )
            if result is _SKIP_ACTION:
                return None  # the receipt is already recorded; stop here
            if result is not None:
                return result
        elif self._stuck.no_progress_streak == 0:
            # No signal and this receipt taught something new: the run left
            # the loop (or never was in one).  The loop-teaching identity
            # resets, so a *later* loop is a fresh loop (#369).  Inside a
            # loop the identity is not reset — a same-signature repeat must
            # still kill the run after it was taught once.
            self._last_progress_at = self._clock()
            self._loop_taught.clear()

        verdict = self._controller.reconcile(self._active_step, receipt)

        verdict = self._controller.reconcile(self._active_step, receipt)
        self._append(
            EventKind.STEP_VERDICT,
            EventSource.GATE,
            _verdict_payload(verdict),
            step_id=self._current_step_id(),
        )
        self._note_step_verdict(self._current_step_id(), verdict)
        # V0 is receipt-level.  It becomes an obligation only when the active
        # step actually expects something; a failed exploratory read must not
        # poison delivery forever.
        obligation_receipt = bool(
            self._active_step is not None and self._active_step.expects
        )
        step_verdicts: tuple[Verdict, ...] = ()
        if obligation_receipt:
            self._verdicts.append(verdict)
            step_verdicts = (verdict,)
        if verdict.status is not VerdictStatus.VIOLATED:
            step_verdicts += self._close_active_step()
        exhausted = self._record_attempts(step_verdicts)
        self._sync_debt(step_verdicts)
        if exhausted:
            self._release_step_with_gaps(
                exhausted, "repair budget exhausted"
            )
            return None
        if any(
            item.status is VerdictStatus.VIOLATED for item in step_verdicts
        ):
            self._transition(TaskPhase.REPAIRING, verdict.reason or "violated")
        self._transition(TaskPhase.RUNNING, "next action")
        return None

    def _release_active_step(self, reason: str) -> TaskResult | None:
        """Close the active step even when its expectations are unmet.

        Releasing ends exploration, not verification: unresolved expectations
        become debt and exhaust the bounded repair budget, so a released step
        can never turn into a delivery on its own.
        """

        step = self._active_step
        if step is None:
            return None
        verdicts = self._controller.close_step(step, self._readonly)
        for verdict in verdicts:
            self._verdicts.append(verdict)
            self._append(
                EventKind.STEP_VERDICT,
                EventSource.GATE,
                _verdict_payload(verdict),
                step_id=str(step.id),
            )
            self._note_step_verdict(step.id, verdict)
        self._closed_steps.add(step.id)
        self._append_step_verified(step)
        self._append(
            EventKind.STEP_CLOSED,
            EventSource.GATE,
            {
                "step_id": step.id,
                "status": "released",
                "reason": reason,
                "verdicts": [_verdict_payload(item) for item in verdicts],
            },
            step_id=str(step.id),
        )
        self._advance_step()
        exhausted = self._record_attempts(verdicts)
        self._sync_debt(verdicts)
        if exhausted:
            self._release_step_with_gaps(
                exhausted, "repair budget exhausted"
            )
            return None
        if any(
            item.status is VerdictStatus.VIOLATED for item in verdicts
        ):
            self._transition(TaskPhase.REPAIRING, reason or "step released")
        self._transition(TaskPhase.RUNNING, "step released")
        return None

    def _release_step_with_gaps(
        self, gaps: Sequence[str], reason: str
    ) -> None:
        """Retire a step whose repair budget ran out and keep the loop moving.

        The gap is not erased: the verdicts already opened debt and the next
        step (or the next plan round) sees it.  Killing the whole turn here is
        what made one stubborn step end a run that had real work behind it.
        """

        step = self._active_step
        payload = {
            "decision": "release_step_with_gaps",
            "gaps": list(gaps),
            "reason": reason,
        }
        self._append(
            EventKind.CONTROL_DECISION,
            EventSource.GATE,
            payload,
            step_id=self._current_step_id(),
        )
        if step is not None:
            # Scheduling must treat a released step as done; the ledger still
            # records that it was released with gaps, not satisfied.
            self._closed_steps.add(step.id)
        self._loop_notes.append(
            "A step was released with gaps: "
            f"{', '.join(gaps[:6]) or reason}. Address these in the next step; "
            "the task audit will re-open a plan round if they remain."
        )
        del self._loop_notes[:-5]
        if step is not None:
            self._advance_step()
        self._transition(TaskPhase.RUNNING, "step released with gaps")

    def _close_implicit_step(self) -> tuple[Verdict, ...]:
        """V1 for turns without a plan (#420).

        `direct`/`react` have no plan steps, so step-level verification had no
        home and never ran.  The turn itself is the step: every edit it made
        must have produced a file-change fact.  Same resolver, same event shape,
        same "matching, never judging" rule.
        """

        if self._plan_steps or not self._edit_steps:
            return ()
        verdicts: list[Verdict] = []
        for step_id, obligation in self._edit_steps:
            verdict = self._controller._obligation_verdict(obligation, self._readonly)
            self._verdicts.append(verdict)
            self._append(
                EventKind.STEP_VERDICT,
                EventSource.GATE,
                _verdict_payload(verdict),
                step_id=str(step_id),
            )
            verdicts.append(verdict)
        self._sync_debt(verdicts)
        return tuple(verdicts)

    def _close_active_step(self) -> tuple[Verdict, ...]:
        step = self._active_step
        if step is None or step.id in self._closed_steps:
            return ()
        verdicts = self._close_step(step)
        if step.id in self._closed_steps:
            self._advance_step()
        return verdicts

    def _close_step(self, step: StepContract) -> tuple[Verdict, ...]:
        verdicts = self._controller.close_step(
            step, self._readonly, since_seq=self._plan_since_seq
        )
        for verdict in verdicts:
            self._verdicts.append(verdict)
            self._append(
                EventKind.STEP_VERDICT,
                EventSource.GATE,
                _verdict_payload(verdict),
                step_id=str(step.id),
            )
            self._note_step_verdict(step.id, verdict)
        satisfied = all(
            item.status is VerdictStatus.SATISFIED for item in verdicts
        )
        if satisfied:
            self._closed_steps.add(step.id)
            self._append_step_verified(step)
            self._append(
                EventKind.STEP_CLOSED,
                EventSource.GATE,
                {
                    "step_id": step.id,
                    "status": "satisfied",
                    "verdicts": [
                        _verdict_payload(item) for item in verdicts
                    ],
                },
                step_id=str(step.id),
            )
        return verdicts

    def _advance_step(self) -> None:
        self._active_step = self._select_ready_step()
        if self._active_step is not None:
            # A new step is new work: the previous step's no-progress streak
            # must not be charged against it.
            self._stuck.reset()
            self._append(
                EventKind.STEP_STARTED,
                EventSource.RUNTIME,
                self._step_payload(self._active_step),
                step_id=self._current_step_id(),
            )

    def _select_ready_step(self) -> StepContract | None:
        """Pick the first plan-ordered step whose dependencies are closed.

        This is a deterministic serial scheduler.  True parallel execution is
        a child-runtime concern (#204); it must not be faked by running two
        steps against the same workspace in this loop.
        """

        for step in self._plan_steps:
            if step.id in self._closed_steps:
                continue
            if all(
                dependency in self._closed_steps
                for dependency in step.depends_on
            ):
                return step
        return None

    def _close_remaining_steps(self, *, skip_commit: bool = False) -> None:
        for step in self._plan_steps:
            if step.id in self._closed_steps:
                continue
            if skip_commit and _is_commit_step(step):
                continue
            self._sync_debt(self._close_step(step))
        if all(step.id in self._closed_steps for step in self._plan_steps):
            self._active_step = None

    def _step_payload(self, step: StepContract) -> dict[str, Any]:
        return _serialize_step(step, task_id=self._task_id())

    def _task_id(self) -> str:
        return self._spec.task_id if self._spec is not None else ""

    def _verified_paths(self) -> tuple[str, ...]:
        if self._spec is None:
            return ()
        paths: set[str] = set()
        for fact in index_facts(self._ledger):
            if fact.seq <= self._run_start_seq:
                continue
            effect = fact.effect
            if isinstance(effect, FsWrite) and effect.changed:
                paths.update(effect.paths)
            elif isinstance(effect, FsDelete) and effect.changed:
                paths.update(effect.paths)
        return tuple(sorted(paths))

    def _probe_worktree(self) -> None:
        """Record one deterministic worktree observation, at most once per state.

        Without it, ``no_stray_changes`` can only ever abstain — the harness
        would be admitting it cannot tell even though the host can.  The probe
        is injected (the kernel never runs git) and the result becomes a
        receipt effect, so the answer is replayable from the ledger alone.
        """

        if self._worktree_probe is None or self._spec is None:
            return
        verified = tuple(self._verified_paths())
        try:
            probe = self._worktree_probe.probe(verified_paths=verified)
        except Exception:  # noqa: BLE001 — a broken probe must not block a run
            logger.warning("worktree probe failed", exc_info=True)
            return
        if probe is None:
            # No VCS here: record nothing, so the resolver honestly abstains.
            return
        signature = f"{probe.head}:{probe.dirty_paths}:{probe.verified_paths}"
        if signature == self._probed_signature:
            return
        self._probed_signature = signature
        from xenon.ledger.events import effect_to_payload

        self._append(
            EventKind.WORKSPACE_PROBED,
            EventSource.RUNTIME,
            effect_to_payload(probe),
            step_id=self._current_step_id(),
        )

    def _start_next_round(
        self, strategy: Strategy, gaps: Sequence[str], reason: str
    ) -> None:
        """Begin another plan round with the previous round's gaps as input."""

        self._transition(TaskPhase.REPLANNING, reason or "plan round gaps")
        self._append(
            EventKind.CONTROL_DECISION,
            EventSource.GATE,
            {
                "decision": "start_next_plan_round",
                "round": self._plan_rounds + 1,
                "gaps": list(gaps),
                "reason": reason,
            },
        )
        # Round-scoped state only.  The ledger keeps every prior verdict; the
        # in-memory latest map must not let a superseded violation decide the
        # new round.
        self._closed_steps.clear()
        self._plan = None
        self._plan_steps = ()
        self._active_step = None
        self._verdicts.clear()
        self._step_verdicts.clear()
        self._repair_attempts.clear()
        self._audit_rounds = 0
        self._forced_finish = False
        self._forced_finish_allowance = 0
        self._stuck.reset()
        self._loop_notes.append(
            f"Plan round {self._plan_rounds + 1}: the previous round left "
            f"these gaps: {', '.join(gaps[:8]) or 'unverified work'}. "
            "Plan to close them; do not repeat actions that produced no fact."
        )
        del self._loop_notes[:-5]
        reset = getattr(strategy, "reset_for_round", None)
        if callable(reset):
            reset()
        self._transition(TaskPhase.RUNNING, "plan round started")

    def _finish(self, action: Finish, strategy: Strategy) -> TaskResult | None:
        if self._spec is not None and (
            self._audit_rounds >= self._max_audit_rounds()
        ):
            return self._budget_block(
                "audit budget exhausted", ("audit",)
            )
        self._audit_rounds += 1
        claims = tuple(self._baseline_claim(item) for item in action.claims)
        for claim in claims:
            self._append(
                EventKind.CLAIM_PROPOSED,
                EventSource.LLM,
                _claim_payload(claim),
            )

        self._close_implicit_step()
        # A Finish is also the last chance to close a plan.  Unexecuted steps
        # produce V1 violations, not an implicit "done".  Commit steps wait for
        # the finalizer so the commit is created from verified paths.
        self._close_remaining_steps(skip_commit=self._finalizer is not None)
        self._sync_debt(self._verdicts)
        self._probe_worktree()
        obligations = self._audit_obligations()
        proposal = self._collect_audit_proposal(obligations)
        preflight_audit = self._audit.audit(
            obligations,
            self._readonly,
            proposal=proposal,
            defer_commit=True,
        )
        self._append_audit_report(preflight_audit, claims, scope="preflight")
        self._verdicts.extend(preflight_audit.verdicts)
        for verdict in preflight_audit.verdicts:
            # Every level writes the same event shape, so `/evidence` and a
            # replay can answer "were all three levels run?" without digging
            # into audit payloads (#420).
            self._append(
                EventKind.STEP_VERDICT,
                EventSource.GATE,
                _verdict_payload(verdict),
            )
        self._sync_debt(preflight_audit.verdicts)
        # #420: a task-level gap is the discrepancy the self-evolution layer
        # exists for — record it as a lesson candidate, never as a block.
        for verdict in preflight_audit.verdicts:
            if verdict.status is VerdictStatus.VIOLATED:
                self._propose_lesson(
                    "claim_fact_mismatch",
                    {
                        "obligation_id": verdict.obligation_id,
                        "reason": verdict.reason or "no matching fact",
                    },
                )

        finalizer_outcome = None
        if self._finalizer is not None and self._preflight_delivery(claims):
            finalizer_outcome = self._invoke_finalizer(claims)
        self._close_remaining_steps()
        self._sync_debt(self._verdicts)

        if (
            self._spec is not None
            and self._spec.mode is not TaskMode.CHAT
            and not claims
            and not self._has_successful_receipt()
            and not self._has_workspace_evidence()
            # A task that never needed a tool is a conversation, not a failed
            # delivery: the plan came back empty and the model answered.  The
            # answer is still UNVERIFIED; there is simply no receipt to demand.
            and not self._answered_without_actions()
        ):
            no_evidence = Verdict(
                obligation_id="task-evidence",
                stage=VerificationStage.V2,
                status=VerdictStatus.UNVERIFIABLE,
                reason="no successful tool receipt for this task",
            )
            self._verdicts.append(no_evidence)
            self._sync_debt((no_evidence,))
            self._transition(TaskPhase.BLOCKED, no_evidence.reason)
            return self._result(
                accepted=False,
                evidence=EvidenceStatus.UNVERIFIED,
                # The answer is still shown; the missing receipt is the
                # disclosure.  An empty screen was the old "block" (#423).
                answer=action.answer,
                claims=claims,
            )

        conversational = self._answered_without_actions() and bool(action.answer)
        if conversational:
            # Nothing to verify because nothing was asked to happen.  Deliver
            # the answer with UNVERIFIED evidence: the conversation happened,
            # and no receipt is missing because none was ever needed.
            self._transition(TaskPhase.AUDITING, "conversational answer")
            self._append(
                EventKind.AUDIT_REPORTED,
                EventSource.GATE,
                {
                    "scope": "final",
                    "verdicts": [],
                    "claims": [],
                    "resolutions": [],
                    "coverage": [],
                    "gaps": [],
                    "debt": [],
                    "reason": "no tool work was required",
                },
            )
            self._append(
                EventKind.DELIVERED,
                EventSource.RUNTIME,
                {
                    "answer": action.answer,
                    "claims": [],
                    "verdicts": [],
                    "finalize": {"commit": None, "verified_paths": []},
                },
            )
            self._transition(TaskPhase.DELIVERING, "conversational answer")
            self._transition(TaskPhase.DELIVERED, "conversational answer")
            self._append(
                EventKind.FINALIZED,
                EventSource.RUNTIME,
                {"commit": None, "evidence_status": "unverified"},
            )
            self._transition(TaskPhase.FINALIZED, "finalized")
            return self._result(
                accepted=True,
                evidence=EvidenceStatus.UNVERIFIED,
                answer=action.answer,
                claims=claims,
            )

        self._transition(TaskPhase.AUDITING, "finish requested")
        checks: tuple[Verdict, ...]
        if claims:
            checks = tuple(
                self._controller.audit(claim, self._readonly, tuple(self._debt))
                for claim in claims
            )
        else:
            checks = (
                self._controller.audit(None, self._readonly, tuple(self._debt)),
            )
        self._verdicts.extend(checks)

        resolutions = tuple(
            self._controller.resolve(claim, self._readonly) for claim in claims
        )
        for resolution in resolutions:
            self._append(
                EventKind.CLAIM_RESOLVED,
                EventSource.GATE,
                _resolution_payload(resolution),
            )

        self._sync_debt(self._verdicts)
        final_audit = self._audit.audit(
            self._audit_obligations(), self._readonly, proposal=proposal
        )
        self._verdicts.extend(final_audit.verdicts)
        self._sync_debt(final_audit.verdicts)
        self._finalize_decision = self._run_finalize()
        self._append(
            EventKind.AUDIT_REPORTED,
            EventSource.GATE,
            {
                "scope": "final",
                "verdicts": [_verdict_payload(item) for item in checks],
                "claims": [_claim_payload(item) for item in claims],
                "resolutions": [
                    _resolution_payload(item) for item in resolutions
                ],
                "coverage": _coverage_payload(final_audit.coverage),
                "gaps": _gaps_payload(final_audit.gaps),
                "debt": [item.obligation.id for item in self._debt],
            },
        )

        decision = self._controller.decide(
            _latest_verdicts(self._verdicts), tuple(self._debt)
        )
        self._append(
            EventKind.CONTROL_DECISION,
            EventSource.GATE,
            {
                "kind": decision.kind.value,
                "reason": decision.reason,
                "obligation_ids": list(decision.obligation_ids),
            },
        )

        gaps = list(decision.obligation_ids) or [
            item.obligation.id for item in self._debt
        ]
        if (
            decision.kind in (
                ControlDecisionKind.BLOCK,
                ControlDecisionKind.ABSTAIN,
            )
            and gaps
            and self._plan is not None
            and self._plan_rounds < self._max_plan_rounds()
            and getattr(strategy, "planner", None) is not None
            and callable(getattr(strategy, "reset_for_round", None))
        ):
            # The audit is feedback, not a verdict on the turn: spend another
            # plan round on the gaps.  After the cap the answer is returned
            # with the gaps disclosed (below).
            self._start_next_round(strategy, gaps, decision.reason)
            return None

        if decision.kind is not ControlDecisionKind.DELIVER:
            finalize = self._finalize_decision
            # Roll back only for *integrity*: a commit containing a path that
            # was never verified, or content that changed after verification.
            # An incomplete obligation is disclosed, not used to erase work.
            integrity_failure = bool(
                getattr(finalize, "hash_mismatches", ())
                or getattr(finalize, "unverified_paths", ())
            )
            if integrity_failure:
                rollback_block = self._rollback_delivery(finalizer_outcome)
                if rollback_block is not None:
                    return rollback_block

        if decision.kind is ControlDecisionKind.DELIVER:
            assert self._spec is not None
            finalize = self._finalize_decision
            assert finalize is not None and finalize.accepted
            self._transition(TaskPhase.DELIVERING, decision.reason)
            self._append(
                EventKind.DELIVERED,
                EventSource.RUNTIME,
                {
                    "answer": action.answer,
                    "claims": [_claim_payload(item) for item in claims],
                    "verdicts": [_verdict_payload(item) for item in checks],
                    "finalize": {
                        "commit": finalize.commit_sha,
                        "verified_paths": list(finalize.verified_paths),
                    },
                },
            )
            self._transition(TaskPhase.DELIVERED, "accepted")
            sha = finalize.commit_sha or self._last_commit_sha()
            evidence = self._evidence_status(checks)
            self._append(
                EventKind.FINALIZED,
                EventSource.RUNTIME,
                {"commit": sha, "evidence_status": evidence.value},
            )
            self._transition(TaskPhase.FINALIZED, "finalized")
            return self._result(
                accepted=True,
                evidence=evidence,
                answer=action.answer,
                claims=claims,
                resolutions=resolutions,
                finalize_sha=sha,
            )

        if decision.kind in (
            ControlDecisionKind.REPAIR,
            ControlDecisionKind.REPLAN,
        ):
            target = (
                TaskPhase.REPAIRING
                if decision.kind is ControlDecisionKind.REPAIR
                else TaskPhase.REPLANNING
            )
            self._transition(target, decision.reason or "audit requested revision")
            self._transition(TaskPhase.RUNNING, "continue after audit")
            return None

        if decision.kind is ControlDecisionKind.ADVANCE:
            self._transition(TaskPhase.RUNNING, "audit requested more work")
            return None

        if decision.kind is ControlDecisionKind.ABSTAIN:
            self._transition(TaskPhase.BLOCKED, decision.reason or "abstained")
            return self._result(
                accepted=False,
                evidence=EvidenceStatus.ABSTAINED,
                # The model's answer is still the answer.  A gap in evidence
                # must be disclosed next to it, never replace it with an empty
                # screen doing to be a delivery (#423).
                answer=action.answer,
                claims=claims,
                resolutions=resolutions,
            )

        if decision.kind is ControlDecisionKind.ABORT:
            self._transition(TaskPhase.ABORTED, decision.reason or "aborted")
            return self._result(
                accepted=False,
                evidence=EvidenceStatus.UNVERIFIED,
                answer=action.answer,
                claims=claims,
                resolutions=resolutions,
            )

        self._transition(TaskPhase.BLOCKED, decision.reason or "blocked")
        return self._result(
            accepted=False,
            evidence=EvidenceStatus.UNVERIFIED,
            answer=action.answer,
            claims=claims,
            resolutions=resolutions,
        )

    def _invoke_finalizer(self, claims: Sequence[Claim]) -> Any:
        if self._spec is None or self._finalizer is None:
            return None
        if self._spec.mode is TaskMode.CHAT:
            return None
        latest = _latest_verdict_map(self._verdicts)
        if self._debt or any(
            item.status is not VerdictStatus.SATISFIED
            for item in latest.values()
        ):
            return None
        existing = self._last_commit_sha()
        # Do not create a commit for a model claim that is already false.
        # A COMMIT_CREATED claim is the one claim the finalizer itself can
        # satisfy, so defer only that one until after the commit exists.
        for claim in claims:
            if (
                claim.kind is PropositionKind.COMMIT_CREATED
                and not existing
            ):
                continue
            resolution = self._controller.resolve(claim, self._readonly)
            if resolution.status is not ResolutionStatus.BOUND:
                return None
        verified = self._verified_paths()
        if not verified and not existing:
            return None
        self._append(
            EventKind.DELIVERY_ATTEMPTED,
            EventSource.RUNTIME,
            {
                "status": "intent",
                "verified_paths": list(verified),
                "existing_commit": existing,
                "claims": [item.id for item in claims],
            },
        )
        try:
            outcome = self._finalizer.finalize(
                verified_paths=verified,
                objective=self._spec.objective,
                task_id=self._spec.task_id,
                existing_commit_sha=existing,
            )
        except Exception as exc:  # noqa: BLE001 — finalizer failure is a block
            verdict = Verdict(
                obligation_id="finalizer",
                stage=VerificationStage.V2,
                status=VerdictStatus.VIOLATED,
                reason=f"finalizer error: {exc}",
            )
            self._verdicts.append(verdict)
            self._sync_debt((verdict,))
            return None
        if not outcome.accepted:
            verdict = Verdict(
                obligation_id="finalizer",
                stage=VerificationStage.V2,
                status=(
                    VerdictStatus.UNVERIFIABLE
                    if getattr(outcome, "unavailable", False)
                    else VerdictStatus.VIOLATED
                ),
                reason=outcome.reason or "finalizer rejected",
            )
            self._verdicts.append(verdict)
            self._sync_debt((verdict,))
            return outcome
        if outcome.created and outcome.commit_sha:
            files = tuple(outcome.files)
            receipt = ToolReceipt(
                receipt_id=f"finalizer-{outcome.commit_sha[:12]}",
                call_id="finalizer",
                task_id=self._spec.task_id,
                tool="finalizer",
                status=ReceiptStatus.OK,
                step_id=self._current_step_id(),
                effects=(VcsCommit(sha=outcome.commit_sha, files=files),),
                declared_effects=(EffectKind.VCS_COMMIT,),
                observed_effects=(EffectKind.VCS_COMMIT,),
            )
            self._append(
                EventKind.TOOL_RECEIPT,
                EventSource.TOOL,
                receipt_event_payload(receipt),
                step_id=self._current_step_id(),
            )
        return outcome

    def _rollback_delivery(self, outcome: Any) -> TaskResult | None:
        if outcome is None:
            return None
        if not getattr(outcome, "created", False):
            return None
        sha = str(getattr(outcome, "commit_sha", "") or "")
        if not sha:
            return None
        rollback = getattr(self._finalizer, "rollback", None)
        if not callable(rollback):
            return self._delivery_rollback_failed(
                sha, "finalizer does not expose rollback"
            )
        try:
            ok, reason = rollback(sha)
        except Exception as exc:  # noqa: BLE001 — rollback failure is blocking
            return self._delivery_rollback_failed(
                sha, f"rollback raised: {exc}"
            )
        self._append(
            EventKind.DELIVERY_ATTEMPTED,
            EventSource.RUNTIME,
            {
                "status": "rolled_back" if ok else "rollback_failed",
                "commit": sha,
                "reason": reason,
            },
        )
        if ok:
            return None
        return self._delivery_rollback_failed(sha, reason or "rollback failed")

    def _delivery_rollback_failed(
        self, sha: str, reason: str
    ) -> TaskResult:
        verdict = Verdict(
            obligation_id="delivery-rollback",
            stage=VerificationStage.V2,
            status=VerdictStatus.VIOLATED,
            reason=f"commit {sha} could not be rolled back: {reason}",
        )
        self._verdicts.append(verdict)
        self._sync_debt((verdict,))
        self._transition(TaskPhase.BLOCKED, verdict.reason)
        return self._result(
            accepted=False,
            evidence=EvidenceStatus.UNVERIFIED,
        )

    def _preflight_delivery(self, claims: Sequence[Claim]) -> bool:
        """Check everything that can be decided before the delivery commit.

        ``COMMIT_CREATED`` is deferred: the finalizer is the only component
        that can create that fact.  Every other claim, all open debt and every
        current verdict must already be settled.
        """

        if self._spec is None or self._finalizer is None:
            return False
        if self._spec.mode is TaskMode.CHAT:
            return False
        latest = _latest_verdict_map(self._verdicts)
        if self._debt or any(
            item.status is not VerdictStatus.SATISFIED
            for item in latest.values()
        ):
            return False
        existing = self._last_commit_sha()
        for claim in claims:
            if claim.kind is PropositionKind.COMMIT_CREATED and not existing:
                continue
            resolution = self._controller.resolve(claim, self._readonly)
            if resolution.status is not ResolutionStatus.BOUND:
                return False
        return True

    def _collect_audit_proposal(
        self, obligations: Sequence[Obligation]
    ) -> AuditProposal | None:
        if self._audit_provider is None or not obligations:
            return None
        try:
            proposal = self._audit_provider.propose(
                tuple(obligations), self._readonly
            )
        except Exception as exc:  # noqa: BLE001 — proposal failure is not a pass
            self._append(
                EventKind.AUDIT_PROPOSED,
                EventSource.LLM,
                {"status": "error", "reason": str(exc)},
            )
            return None
        if proposal is None:
            return None
        if not isinstance(proposal, AuditProposal):
            self._append(
                EventKind.AUDIT_PROPOSED,
                EventSource.LLM,
                {
                    "status": "error",
                    "reason": "provider returned a non-AuditProposal value",
                },
            )
            return None
        self._append(
            EventKind.AUDIT_PROPOSED,
            EventSource.LLM,
            {
                "status": "proposed",
                "coverage": [
                    {
                        "obligation_id": item.obligation_id,
                        "artifact_paths": list(item.artifact_paths),
                        "test_targets": list(item.test_targets),
                        "rationale": item.rationale,
                    }
                    for item in proposal.coverage
                ],
                "gaps": _gaps_payload(proposal.gaps),
                "rationale": proposal.rationale,
            },
        )
        return proposal

    def _append_audit_report(
        self, report: Any, claims: Sequence[Claim], *, scope: str
    ) -> None:
        self._append(
            EventKind.AUDIT_REPORTED,
            EventSource.GATE,
            {
                "scope": scope,
                "claims": [_claim_payload(item) for item in claims],
                "verdicts": [
                    _verdict_payload(item) for item in report.verdicts
                ],
                "coverage": _coverage_payload(report.coverage),
                "gaps": _gaps_payload(report.gaps),
                "debt": [item.obligation.id for item in self._debt],
            },
        )

    def _audit_obligations(self) -> tuple[Obligation, ...]:
        """All obligations V2 must cover, in deterministic order."""

        found: list[Obligation] = []
        seen: set[str] = set()

        def add(obligation: Obligation) -> None:
            if not obligation.id or obligation.id in seen:
                return
            seen.add(obligation.id)
            found.append(self._baseline_obligation(obligation))

        if self._spec is not None:
            for obligation in self._spec.obligations:
                add(obligation)
        for step in self._plan_steps:
            for obligation in step.expects:
                add(obligation)
        # A debt that already carries a *concrete* violated verdict is decided:
        # re-auditing it can only replace the real reason ("worktree contains
        # unverified paths: notes.txt") with a generic "semantic claim needs a
        # coverage binder", which is exactly how a stray change used to be
        # reported.  The verdict still blocks; only the wording improves.
        violated = {
            item.obligation_id
            for item in _latest_verdicts(self._verdicts)
            if item.status is VerdictStatus.VIOLATED
        }
        for debt in self._debt:
            if debt.obligation.id in violated:
                continue
            add(debt.obligation)
        return tuple(found)

    def _run_finalize(self) -> FinalizeDecision:
        assert self._spec is not None
        finalize = self._finalize.check(
            self._readonly,
            since_seq=self._run_start_seq,
            workspace=self._spec.workspace,
            allow_push=self._spec.allow_push,
        )
        self._append(
            EventKind.FINALIZE_STARTED,
            EventSource.RUNTIME,
            {
                "commit": finalize.commit_sha,
                "verified_paths": list(finalize.verified_paths),
                "unverified_paths": list(finalize.unverified_paths),
                "hash_mismatches": list(finalize.hash_mismatches),
                "reason": finalize.reason,
            },
        )
        if not finalize.accepted:
            verdict = Verdict(
                obligation_id="finalize",
                stage=VerificationStage.V2,
                status=VerdictStatus.VIOLATED,
                reason=finalize.reason or "finalize rejected",
            )
            self._verdicts.append(verdict)
            self._sync_debt((verdict,))
        return finalize

    # ── policies and limits ────────────────────────────────────────────

    def _append_step_verified(self, step: StepContract) -> None:
        """One aggregated step-level verification record per step.

        ``step_verdict`` carries individual decisions; this event is the summary
        the turn tree and reflection read, so "what did step verification say"
        is a single lookup instead of a fold over verdict events.
        """

        verdicts = tuple(
            _latest_verdict_map(self._step_verdicts.get(int(step.id), ())).values()
        )
        satisfied = bool(verdicts) and all(
            item.status is VerdictStatus.SATISFIED for item in verdicts
        )
        self._append(
            EventKind.STEP_VERIFIED,
            EventSource.GATE,
            {
                "step_id": step.id,
                "status": "satisfied" if satisfied else "unmet",
                "verdicts": [_verdict_payload(item) for item in verdicts],
            },
            step_id=str(step.id),
        )

    def _note_step_verdict(self, step_id: Any, verdict: Verdict) -> None:
        """Remember a step-level verdict for the model view (#284+)."""

        try:
            key = int(step_id)
        except (TypeError, ValueError):
            return
        self._step_verdicts.setdefault(key, []).append(verdict)

    def _active_step_verdicts(self) -> tuple[Verdict, ...]:
        if self._active_step is None:
            return ()
        latest = _latest_verdict_map(
            self._step_verdicts.get(int(self._active_step.id), ())
        )
        return tuple(latest.values())

    def _record_strategy_notices(self, strategy: Any, action: Action) -> None:
        """Audit strategy-level proposals the runtime does not execute.

        Only reflection uses this today.  The record is informational: it can
        never satisfy an obligation or close a debt.
        """

        drain = getattr(strategy, "drain_notices", None)
        if not callable(drain):
            return
        for notice in drain() or ():
            if not isinstance(notice, Mapping):
                continue
            kind = str(notice.get("kind") or "reflection")
            if kind == "plan":
                self._append(
                    EventKind.PLAN_REVIEWED,
                    EventSource.LLM,
                    {
                        "decision": str(notice.get("decision") or ""),
                        "added_steps": int(notice.get("added_steps") or 0),
                        "reason": str(notice.get("reason") or ""),
                    },
                )
                continue
            if kind == "step":
                self._append(
                    EventKind.STEP_EXPLORED,
                    EventSource.LLM,
                    {
                        "decision": str(notice.get("decision") or ""),
                        "step_id": notice.get("step"),
                        "calls": int(notice.get("calls") or 0),
                    },
                    step_id=(
                        str(notice.get("step"))
                        if notice.get("step") is not None
                        else None
                    ),
                )
                continue
            self._append(
                EventKind.REFLECTION_PROPOSED,
                EventSource.LLM,
                {
                    "decision": str(notice.get("decision") or ""),
                    "capability": str(notice.get("capability") or ""),
                    "round": int(notice.get("round") or 0),
                    "action": action.kind.value,
                },
            )

    def _check_policies(self, view: Any, action: Action) -> Any:
        for policy in self._policies:
            decision = policy.check(view, action)
            if decision.kind is PolicyDecisionKind.ALLOW:
                # A grant answering is a recorded decision, not silence (#373):
                # a replay must explain "why this was not asked".  Routine
                # allows (workspace edits, safe tests) stay quiet as before.
                if str(decision.reason).startswith(
                    ("session grant:", "scoped grant:")
                ):
                    self._append(
                        EventKind.POLICY_DECISION,
                        EventSource.GATE,
                        {
                            "kind": PolicyDecisionKind.ALLOW.value,
                            "reason": decision.reason,
                            "scope": list(decision.scope),
                            "ttl_seconds": decision.ttl_seconds,
                        },
                    )
                continue
            self._append(
                EventKind.POLICY_DECISION,
                EventSource.GATE,
                {
                    "kind": decision.kind.value,
                    "reason": decision.reason,
                    "scope": list(decision.scope),
                    "ttl_seconds": decision.ttl_seconds,
                },
            )
            if decision.kind is PolicyDecisionKind.REQUIRE_APPROVAL:
                approved, approval_reason = self._request_approval(
                    view, action, decision
                )
                self._append(
                    EventKind.USER_APPROVAL,
                    EventSource.USER,
                    {
                        "approved": approved,
                        "reason": approval_reason,
                        "scope": list(decision.scope),
                        "tool": (
                            action.intent.capability.value
                            if isinstance(action, CallTool)
                            else type(action).__name__
                        ),
                    },
                )
                if not approved:
                    # Self-evolution (slice 2): a human refusal is a preference
                    # signal.  Propose, do not store — the model never writes
                    # its own lesson.
                    self._propose_lesson(
                        "user_approval",
                        {
                            "approved": False,
                            "tool": (
                                action.intent.capability.value
                                if isinstance(action, CallTool)
                                else type(action).__name__
                            ),
                            "reason": approval_reason,
                        },
                    )
                if approved and self._grant_approval(
                    policy, decision, action
                ):
                    continue
                if approved:
                    approval_reason = (
                        "approval could not be scoped to a target; "
                        "denied fail-closed"
                    )
                self._revoke_grants()
                # A refusal is an outcome of *this action*, not of the task.
                # Returning a blocked result here is what made a single denied
                # shell call kill a multi-step run (#423).  The action still
                # never executes; the loop just gets a chance to choose another
                # route (search/read/edit, or ask the user).
                blocked = self._refuse_and_continue(
                    action,
                    approval_reason or "approval denied",
                    EventSource.USER,
                )
                return _POLICY_REFUSED if blocked is None else blocked
            self._revoke_grants()
            blocked = self._refuse_and_continue(
                action,
                decision.reason or "policy blocked",
                EventSource.GATE,
            )
            return _POLICY_REFUSED if blocked is None else blocked
        return None

    def _refuse_and_continue(
        self, action: Action, reason: str, source: EventSource
    ) -> TaskResult | None:
        """Record a refused action and let the loop try a different one.

        A refusal is not a task failure, but a run that only ever hits the same
        wall must still stop: the cap keeps "continue, don't block" from
        becoming an infinite loop.
        """

        capability = (
            action.intent.capability.value
            if isinstance(action, CallTool)
            else type(action).__name__
        )
        self._note_refusal(reason, capability)
        key = f"{capability}:{reason.split(':')[0][:60]}"
        count = self._refusals.get(key, 0)
        self._loop_notes.append(
            f"{capability}: refused by policy ({reason}). This action did not "
            "run. Do not repeat it; choose a different capability/target or "
            "ask the user."
        )
        del self._loop_notes[:-5]
        self._append(
            EventKind.CONTROL_DECISION,
            source,
            {
                "decision": "refuse",
                "reason": reason,
                "scope": [capability],
                "continued": count < _MAX_POLICY_REFUSALS,
                "refusal_count": count,
            },
            step_id=self._current_step_id(),
        )
        if count < _MAX_POLICY_REFUSALS:
            return None
        self._transition(
            TaskPhase.BLOCKED,
            f"policy refused {capability} {count} times: {reason}",
        )
        return self._result(
            accepted=False,
            evidence=EvidenceStatus.ABSTAINED,
        )

    def _request_escalation(
        self, view: Any, action: RequestEscalation
    ) -> TaskResult | None:
        """A model's formal request for one call this turn would refuse (#392).

        Order matters: hard rules first (never prompt for the un-approvable),
        then plan mode (a plan-only turn must have no side effects, so asking
        there would break the contract), then the terminal check, and only then
        the human.  An approval becomes a one-shot ``ScopedGrant`` that the
        next ``call_tool`` consumes and the gateway call's ``finally`` revokes.
        """

        capability = action.intent.capability
        # The policy owns the authoritative target set (``approve()`` re-derives
        # it); this copies the model's own declaration only so the ledger can
        # show *what* was requested alongside *why*.
        scope = (
            capability.value,
            *tuple(str(item) for item in action.intent.targets),
        )

        def _refuse(reason: str) -> TaskResult:
            self._append(
                EventKind.POLICY_DECISION,
                EventSource.GATE,
                {
                    "kind": PolicyDecisionKind.VETO.value,
                    "reason": reason,
                    "scope": list(scope),
                },
            )
            self._transition(TaskPhase.BLOCKED, reason)
            return self._result(
                accepted=False, evidence=EvidenceStatus.ABSTAINED
            )

        # Plan mode never reaches this handler: the loop refuses any non-plan
        # action in a plan-only task *before* dispatch, so there is nothing to
        # escalate and no one is asked.  Kept out of here on purpose — a second
        # check would be unreachable code pretending to be a guard.
        for policy in self._policies:
            refuse = getattr(policy, "refuse_escalation", None)
            if not callable(refuse):
                continue
            try:
                hard = str(refuse(action.intent) or "")
            except Exception:  # noqa: BLE001 — a broken policy fails closed
                hard = "policy could not evaluate the escalation"
            if hard:
                return _refuse(hard)
        if self._approval is None:
            return _refuse(
                "escalation needs an approver: no interactive terminal; "
                "re-run with --permission bypass or from a TTY"
            )

        call = CallTool(action.intent)
        decision = PolicyDecision(
            kind=PolicyDecisionKind.REQUIRE_APPROVAL,
            reason=f"escalation requested: {action.reason}",
            scope=scope,
            ttl_seconds=None,
        )
        approved, approval_reason = self._request_approval(view, call, decision)
        self._append(
            EventKind.USER_APPROVAL,
            EventSource.USER,
            {
                "approved": approved,
                "reason": approval_reason,
                "scope": list(scope),
                "tool": capability.value,
                "escalation": True,
            },
        )
        if not approved:
            self._propose_lesson(
                "user_approval",
                {
                    "approved": False,
                    "tool": capability.value,
                    "reason": approval_reason,
                },
            )
            return _refuse(
                f"escalation denied ({capability.value}): {approval_reason}"
            )
        if not self._grant_for_escalation(decision, call):
            return _refuse(
                "escalation approved but could not be scoped to a target; "
                "denied fail-closed"
            )
        # The call itself happens on the *next* action, so the model sees the
        # granted boundary in a fresh view instead of us executing implicitly.
        # A grant is bounded to the exact call: say so, or the model refines the
        # command (real run: it added `$ProgressPreference=...`) and the grant
        # no longer matches — a correct refusal that reads as a mystery.
        detail = ""
        for policy in self._policies:
            describe = getattr(policy, "escalation_scope", None)
            if callable(describe):
                try:
                    detail = str(describe(action.intent) or "")
                except Exception:  # noqa: BLE001 — a note must not fail a run
                    detail = ""
                if detail:
                    break
        self._loop_notes.append(
            f"Escalation approved for exactly this call: "
            f"{capability.value}"
            + (f" `{detail[:160]}`" if detail else "")
            + ". Propose that exact call next; any other command is refused."
        )
        del self._loop_notes[:-5]
        self._transition(TaskPhase.RUNNING, f"escalation granted: {capability.value}")
        return None

    def _grant_for_escalation(self, decision: Any, action: Action) -> bool:
        granted = False
        for policy in self._policies:
            if self._grant_approval(policy, decision, action):
                granted = True
        return granted

    def _request_approval(
        self, view: Any, action: Action, decision: Any
    ) -> tuple[bool, str]:
        if self._approval is None:
            return False, "no approval provider; denied fail-closed"
        try:
            approved = bool(self._approval.request(view, action, decision))
        except Exception as exc:  # noqa: BLE001 — approval UI must fail closed
            logger.warning("approval provider failed", exc_info=True)
            return False, f"approval provider failed: {exc}"
        if approved:
            scope = str(getattr(self._approval, "last_scope", "") or "")
            if scope in {"session", "scoped"}:
                self._remember_grant(action, decision, scope)
            return True, decision.reason or "user approval"
        # The provider knows *why* it said no (rejected / cancelled / no UI).
        # Losing that turns a blocked run into a mystery.
        return False, str(
            getattr(self._approval, "last_reason", "")
            or decision.reason
            or "user denied approval"
        )

    def _remember_grant(
        self, action: Action, decision: Any, scope: str
    ) -> None:
        """Record that the human chose to be asked once, not every time.

        The event is the point: a session with fewer prompts must still be
        explainable from its ledger, and the grant itself must be a *recorded
        decision* rather than a hidden setting.  It is never evidence — it does
        not touch claims, verdicts or the evidence status.
        """

        from xenon.kernel.session_grants import (
            SCOPED,
            SESSION,
            grant_from_choice,
        )

        intent = getattr(action, "intent", None)
        capability = getattr(intent, "capability", None)
        if capability is None:
            return
        spec = self._spec
        workspace = spec.workspace if spec is not None else ""
        targets = tuple(str(item) for item in getattr(decision, "scope", ())[1:])
        grant = grant_from_choice(
            SESSION if scope == "session" else SCOPED,
            capability=capability,
            targets=targets,
            workspace=workspace,
            reason=str(getattr(decision, "reason", "") or ""),
        )
        self._append(
            EventKind.GRANT_ADDED,
            EventSource.USER,
            {
                "capability": capability.value,
                "scope": grant.scope if grant is not None else "",
                "workspace": workspace,
                "answer": scope,
                "accepted": grant is not None,
            },
        )
        if grant is not None:
            scoped = grant.as_scoped()
            for policy in self._policies:
                add = getattr(policy, "grant", None)
                if callable(add):
                    try:
                        add((scoped,))
                    except Exception:  # noqa: BLE001 — a grant must not break a run
                        logger.warning("policy could not take the grant", exc_info=True)
            # Handed to the front-end, which owns the durable book.  The runtime
            # does not keep it: a kernel that stores permission state would make
            # replay depend on something the ledger cannot show.
            self.last_grant = grant

    def _grant_approval(
        self, policy: Any, decision: Any, action: Action
    ) -> bool:
        approve = getattr(policy, "approve", None)
        if not callable(approve):
            # A custom policy may authorize the action itself; no grant is
            # needed.  PermissionPolicy always exposes approve().
            return True
        try:
            grants = tuple(approve(decision, action))
        except Exception:  # noqa: BLE001 — a broken grant must fail closed
            logger.warning("policy grant failed", exc_info=True)
            return False
        if not grants:
            return False
        self._approved_grants.extend(grants)
        return True

    def _revoke_grants(self) -> None:
        if not self._approved_grants:
            return
        grants = tuple(self._approved_grants)
        self._approved_grants.clear()
        for policy in self._policies:
            revoke = getattr(policy, "revoke", None)
            if not callable(revoke):
                continue
            try:
                revoke(grants)
            except Exception:  # noqa: BLE001 — best effort; grants have TTL too
                logger.warning("policy revoke failed", exc_info=True)

    def post_user_input(
        self,
        text: str,
        *,
        kind: str = "constraint",
        scope: str = "task",
        target_seq: int = 0,
    ) -> int:
        """Record one operator input; the loop consumes it at the next boundary.

        This is a write to the ledger, not a mutation of runtime state, so a
        front-end cannot change behaviour by holding a reference — it can only
        add a fact.
        """

        payload = parse_steering(text, kind=kind, scope=scope, target_seq=target_seq)
        event = self._append(EventKind.USER_INPUT, EventSource.USER, payload)
        return int(getattr(event, "seq", 0) or 0)

    def note_usage(self, payload: Mapping[str, Any]) -> None:
        """Fold one provider usage report into the task's spend.

        Called by the harness bridge; also callable directly in tests so the
        accounting path needs no live model.
        """

        normalised = usage_payload(payload)
        self._usage.add(normalised)
        # A model answer is progress even when it takes minutes (v4-pro
        # produced a 50k-token plan and was then mistaken for a stalled run).
        # The stall brake measures "no model output and no new fact", not raw
        # wall time.
        self._last_progress_at = self._clock()
        self._append(
            EventKind.MODEL_USAGE,
            EventSource.LLM,
            {
                **normalised,
                "cumulative_total": self._usage.total,
            },
        )

    def usage(self) -> dict[str, int]:
        return self._usage.as_dict()

    def attach_steering_source(self, source: Any) -> None:
        """Register a callable returning pending operator texts.

        The source is a *transport* (a queue a front-end writes to), never the
        record: whatever it yields is turned into ledger events here, by the
        same single writer that owns every other event.  A front-end therefore
        cannot change behaviour without leaving a trace.
        """

        self._steering_source = source

    def _drain_steering_source(self) -> int:
        source = self._steering_source
        if source is None:
            return 0
        written = 0
        while True:
            try:
                item = source()
            except Exception:  # noqa: BLE001 — a broken UI must not stop a run
                break
            if item is None:
                break
            text = item if isinstance(item, str) else str(item.get("text") or "")
            kind = "constraint" if isinstance(item, str) else str(
                item.get("kind") or "constraint"
            )
            scope = "task" if isinstance(item, str) else str(
                item.get("scope") or "task"
            )
            try:
                self.post_user_input(text, kind=kind, scope=scope)
            except Exception:  # noqa: BLE001 — skip malformed operator input
                continue
            written += 1
        return written

    def _consume_steering(self) -> int:
        """Apply operator inputs written since the last action boundary.

        Reading happens only here, so an input can never land inside a tool
        call: it takes effect at the same place a model action would.
        """

        self._drain_steering_source()
        applied = self._steering.consume(self._readonly.read())
        if applied:
            self._append(
                EventKind.CONTROL_DECISION,
                EventSource.RUNTIME,
                {
                    "kind": "steering",
                    "consumed": applied,
                    "up_to_seq": self._steering.consumed_up_to,
                    "constraints": len(self._steering.constraints),
                },
            )
        return applied

    def steering_state(self) -> SteeringState:
        """Read-only view of what the loop has been told (for UI and tests)."""

        return self._steering

    def _note_session_context(self) -> None:
        """Record what reference material the model was given (once per set).

        The content itself is not copied into the ledger — a memory may be
        private — but labels, sizes and a digest are enough to answer "did the
        rules reach the model, and were they the rules I meant?" during replay.
        """

        blocks = tuple(getattr(self._spec, "context_blocks", ()) or ())
        if not blocks:
            return
        digest = hashlib.sha256(
            chr(0).join(f"{label}{chr(0)}{text}" for label, text in blocks).encode(
                "utf-8"
            )
        ).hexdigest()
        if digest == self._context_digest:
            return
        self._context_digest = digest
        self._append(
            EventKind.SESSION_CONTEXT,
            EventSource.RUNTIME,
            {
                "blocks": [
                    {"label": label, "chars": len(text)} for label, text in blocks
                ],
                "total_chars": sum(len(text) for _label, text in blocks),
                "digest": digest[:16],
            },
        )

    def _note_compaction(self, view: Any) -> None:
        """Record that the working set was folded, once per distinct fold.

        Compaction is deterministic and silent from the model's point of
        view (the digest entry is already in the view), so the ledger needs
        its own trace or the history would look like it simply shrank.
        """

        compacted = tuple(getattr(view, "compacted", ()) or ())
        if not compacted:
            return
        signature = f"{compacted[0]}:{compacted[-1]}:{len(compacted)}"
        if signature == self._compaction_signature:
            return
        self._compaction_signature = signature
        self._append(
            EventKind.CONTEXT_COMPACTED,
            EventSource.RUNTIME,
            {
                "seqs": list(compacted),
                "count": len(compacted),
                "input_seq": getattr(view, "input_seq", 0),
            },
        )

    def _check_limits(self) -> TaskResult | None:
        if self._cancelled:
            self._transition(TaskPhase.ABORTED, "cancelled")
            return self._result(
                accepted=False, evidence=EvidenceStatus.UNVERIFIED
            )
        if self._spec is None:
            return None
        if (
            self._spec.budget.max_steps is not None
            and self._steps >= self._spec.budget.max_steps
        ):
            # Same shape as the token budget below: a blocked run must say why
            # in the ledger.  This branch used to transition without writing a
            # decision, so "step budget exhausted" existed only in the result
            # object and never in the record (#414).
            return self._budget_block(
                "step budget exhausted", ("step_budget",), signal="step_budget"
            )
        limit = self._spec.budget.max_tokens
        if limit and self._usage.total >= int(limit):
            self._transition(TaskPhase.BLOCKED, "token budget exhausted")
            self._append(
                EventKind.POLICY_DECISION,
                EventSource.RUNTIME,
                {
                    "kind": PolicyDecisionKind.VETO.value,
                    "reason": "token budget exhausted",
                    "scope": ["token_budget"],
                    "used": self._usage.total,
                    "limit": int(limit),
                },
            )
            return self._result(
                accepted=False, evidence=EvidenceStatus.UNVERIFIED
            )
        if self._deadline is not None and time.time() > self._deadline:
            self._transition(TaskPhase.BLOCKED, "time budget exhausted")
            return self._result(
                accepted=False, evidence=EvidenceStatus.UNVERIFIED
            )
        if self._stalled():
            reason = self._stall_reason()
            self._append(
                EventKind.POLICY_DECISION,
                EventSource.RUNTIME,
                {
                    "kind": PolicyDecisionKind.VETO.value,
                    "reason": reason,
                    "signal": "stalled",
                },
            )
            self._transition(TaskPhase.BLOCKED, reason)
            return self._result(
                accepted=False, evidence=EvidenceStatus.UNVERIFIED
            )
        return None

    def _stalled(self) -> bool:
        """Time is the gentle brake; steps are the crude one.

        A long task that keeps learning new facts must not be stopped, so this
        only counts *unproductive* time: the clock resets whenever the loop
        detector sees a new fact.  ``max_stall_seconds=None`` disables it.
        """

        if self._spec is None:
            return False
        limit = self._spec.budget.max_stall_seconds
        if limit is None or limit <= 0:
            return False
        return (self._clock() - self._last_progress_at) > float(limit)

    def _stall_reason(self) -> str:
        assert self._spec is not None
        limit = float(self._spec.budget.max_stall_seconds or 0.0)
        diagnosis = self._stuck.diagnosis
        base = f"stalled: {limit:.0f}s without new information"
        return f"{base} [{diagnosis}]" if diagnosis else base

    # ── results and persistence ────────────────────────────────────────

    def _result(
        self,
        *,
        accepted: bool,
        evidence: EvidenceStatus,
        answer: str = "",
        claims: Sequence[Claim] = (),
        resolutions: Sequence[Resolution] = (),
        finalize_sha: str | None = None,
    ) -> TaskResult:
        assert self._spec is not None
        return TaskResult(
            task_id=self._spec.task_id,
            accepted=accepted,
            phase=self._phase,
            answer=answer,
            claims=tuple(claims),
            resolutions=tuple(resolutions),
            verdicts=tuple(self._verdicts),
            debt=tuple(self._debt),
            finalize_sha=finalize_sha,
            evidence_status=evidence,
            metrics={
                "steps": float(self._steps),
                "tool_calls": float(self._tool_calls),
                "events": float(self._ledger.watermark()),
                "repair_attempts": float(sum(self._repair_attempts.values())),
                "replans": float(self._replans),
                "plan_rounds": float(self._plan_rounds),
                "audit_rounds": float(self._audit_rounds),
                # Refusals are a *protocol* signal: one is normal, a pile of
                # them means the catalog and the model disagree about how to
                # call things (#417).
                "refusals": float(sum(self._refusals.values())),
            },
        )

    def _evidence_status(self, checks: Sequence[Verdict]) -> EvidenceStatus:
        assert self._spec is not None
        if self._spec.mode is TaskMode.CHAT:
            return EvidenceStatus.UNVERIFIED
        if self._debt:
            return EvidenceStatus.PARTIAL
        if checks and all(
            item.status is VerdictStatus.SATISFIED for item in checks
        ):
            if any(getattr(item, "from_proposal", False) for item in checks):
                # A proposal pointed at real facts; whether those facts *prove*
                # the requirement is a judgement, so this stays PARTIAL.  This
                # is the difference between "the harness verified it" and "the
                # model said these receipts cover it".
                return EvidenceStatus.PARTIAL
            return EvidenceStatus.VERIFIED
        return EvidenceStatus.PARTIAL

    def _current_step_id(self) -> str | None:
        if self._active_step is None:
            return None
        return str(self._active_step.id)

    def _record_attempts(
        self, verdicts: Sequence[Verdict]
    ) -> tuple[str, ...]:
        latest = _latest_verdict_map(verdicts)
        exhausted: list[str] = []
        for obligation_id, verdict in latest.items():
            if verdict.status is VerdictStatus.SATISFIED:
                continue
            count = self._repair_attempts.get(obligation_id, 0) + 1
            self._repair_attempts[obligation_id] = count
            for index, debt in enumerate(self._debt):
                if debt.obligation.id == obligation_id:
                    self._debt[index] = replace(debt, attempts=count)
            if count > self._max_repairs():
                exhausted.append(obligation_id)
        return tuple(exhausted)

    def _max_repairs(self) -> int:
        if self._spec is None:
            return 0
        return self._spec.budget.max_repair_attempts

    def _max_replans(self) -> int:
        if self._spec is None:
            return 0
        return self._spec.budget.max_replans

    def _max_audit_rounds(self) -> int:
        if self._spec is None:
            return 0
        return max(0, self._spec.budget.max_audit_rounds)

    def _max_plan_rounds(self) -> int:
        if self._spec is None:
            return 1
        return max(1, int(self._spec.budget.max_plan_rounds))

    def _budget_block(
        self, reason: str, scope: Sequence[str] = (), *, signal: str = ""
    ) -> TaskResult:
        # Attach the loop detector's diagnosis so "step budget exhausted" says
        # what the run was actually doing.  The budget still decides the stop;
        # the detector only explains it.
        diagnosis = self._stuck.diagnosis
        if diagnosis and diagnosis not in reason:
            reason = f"{reason} [{diagnosis}]"
        # Since #414 a detected loop no longer blocks the run by itself; it
        # closes the tools and asks for an answer.  If the budget is what
        # finally stops the run, the loop is still the explanation the
        # operator needs, so carry the last teaching note with it.
        if self._forced_finish and self._loop_notes:
            note = self._loop_notes[-1]
            if "loop" not in reason.lower():
                reason = f"{reason} [loop: {note[:200]}]"
        payload: dict[str, Any] = {
            "kind": PolicyDecisionKind.VETO.value,
            "reason": reason,
            "scope": list(scope),
        }
        if signal:
            payload["signal"] = signal
        self._append(
            EventKind.POLICY_DECISION,
            EventSource.GATE,
            payload,
        )
        self._transition(TaskPhase.BLOCKED, reason)
        return self._result(
            accepted=False, evidence=EvidenceStatus.UNVERIFIED
        )

    def _on_loop_signal(
        self,
        kind: str,
        detail: str,
        tool: str,
        reason: str,
        signal: Any = None,
    ) -> TaskResult | None:
        """Teach the model once, then kill the run (#369).

        Every hit writes ``LOOP_DETECTED`` with the structured facts (kind,
        detail, tool) so the ledger can answer "what was the loop doing".  The
        first ``max_loop_corrections`` hits for *this signal* instead inject a
        diagnosis into the next view's ``Notes`` and let the model take a
        corrective turn — evidence, approval and delivery rules are untouched.
        A repeat of the same signal after teaching kills the run with the
        correction count in the reason, so teaching can never become an
        infinite retry.  ``max_loop_corrections=0`` restores the pre-#369
        behaviour exactly.

        Returns ``None`` when the model may continue, or the blocking result.
        """

        # Identity of the loop is kind + window, not the detail text: the
        # detail embeds a counter ("4 times") that would make every hit look
        # like a fresh signal and teaching would never end.
        window = tuple(sorted(signal.window)) if signal is not None else ()
        key = hashlib.sha256(
            f"{kind}|{window}".encode("utf-8")
        ).hexdigest()[:16]
        attempts = self._loop_taught.get(key, 0)
        max_corrections = (
            self._spec.max_loop_corrections
            if self._spec is not None
            else 1
        )
        self._append(
            EventKind.LOOP_DETECTED,
            EventSource.GATE,
            {
                "kind": kind,
                "detail": detail,
                "tool": tool,
                "reason": reason,
                "correction_attempt": attempts,
            },
            step_id=self._current_step_id(),
        )
        self._propose_lesson(
            "loop_detected",
            {"signal": kind, "reason": reason},
        )
        if attempts >= max_corrections:
            # Taught already (or max_loop_corrections=0): same signal again
            # means the model did not change its approach.  The reason states
            # how many corrective turns were offered, per the acceptance.
            if attempts:
                suffix = (
                    f"after {attempts} correction attempt"
                    + ("s" if attempts != 1 else "")
                )
                reason = f"{reason} {suffix}"
            if self._forced_finish:
                # The tools are already closed; the *call gate* decides whether
                # this particular action gets the one allowance (#414).  A
                # second block here would veto the call the gate just allowed.
                return None
            if self._ok_receipts:
                # The run already did real work: killing it here returned
                # "任务未交付" after twelve successful tool calls and threw the
                # evidence away (#407).  Stop the tools, ask for an answer.
                self._forced_finish = True
                # #414: closing the tools once is a remedy, not a sentence.
                # If the wrap-up genuinely needs one more call, allow exactly
                # one — recorded — instead of blocking the agent's own answer.
                self._forced_finish_allowance = 1
                self._append(
                    EventKind.CONTROL_DECISION,
                    EventSource.GATE,
                    {
                        "decision": "force_finish",
                        "reason": reason,
                        "scope": ["loop", tool],
                        "receipts": self._ok_receipts,
                    },
                    step_id=self._current_step_id(),
                )
                self._loop_notes.append(
                    f"Tools are now closed ({reason}). You have "
                    f"{self._ok_receipts} successful receipt(s) in this run: "
                    "answer with what they support and submit `finish`. Do not "
                    "call another tool; unsupported statements will be "
                    "reported as unverified, not silently accepted."
                )
                del self._loop_notes[:-5]
                return _SKIP_ACTION
            return self._budget_block(reason, ("loop", tool), signal=kind)
        self._loop_taught[key] = attempts + 1
        self._loop_notes.append(
            f"Loop detected ({kind}, attempt {attempts + 1}/"
            f"{max_corrections}): {detail}. "
            "Change the approach; the same signal is taught only once."
        )
        # The Notes section must stay bounded: a run that keeps changing walls
        # could otherwise accumulate one note per step and blow the prompt.
        # Only the most recent notes survive; the ledger keeps the full chain.
        del self._loop_notes[:-5]
        return None

    def _sync_debt(self, verdicts: Sequence[Verdict]) -> None:
        latest = _latest_verdict_map(verdicts)
        if not latest:
            return
        remaining: list[Debt] = []
        existing: set[str] = set()
        for debt in self._debt:
            verdict = latest.get(debt.obligation.id)
            if verdict is not None and verdict.status is VerdictStatus.SATISFIED:
                self._append(
                    EventKind.DEBT_CLOSED,
                    EventSource.GATE,
                    {
                        "obligation_id": debt.obligation.id,
                        "reason": "verified by a later fact",
                        "stage": verdict.stage.value,
                    },
                    step_id=self._current_step_id(),
                )
                continue
            remaining.append(debt)
            existing.add(debt.obligation.id)
        self._debt = remaining

        for obligation_id, verdict in latest.items():
            if verdict.status is VerdictStatus.SATISFIED:
                continue
            if (
                verdict.stage is VerificationStage.V0
                and self._find_obligation(obligation_id, verdict) is None
            ):
                # A failed exploratory tool call is not an obligation.  It is
                # still recorded as a receipt/verdict; only plan/claim
                # obligations can block delivery permanently.
                continue
            if obligation_id in existing:
                continue
            obligation = self._find_obligation(obligation_id, verdict)
            if obligation is None:
                obligation = Obligation(
                    id=obligation_id,
                    kind=PropositionKind.REQUIREMENT_MET,
                    source=ObligationSource.USER,
                    required=True,
                    since_seq=self._run_start_seq,
                )
            acceptable = verdict.status is not VerdictStatus.VIOLATED
            debt = Debt(
                obligation=obligation,
                reason=verdict.reason or verdict.status.value,
                attempts=self._repair_attempts.get(obligation_id, 0),
                acceptable=acceptable,
                user_accepted=bool(self._accept_debt) and acceptable,
            )
            self._debt.append(debt)
            existing.add(obligation_id)
            if debt.user_accepted:
                self._append(
                    EventKind.DEBT_ACCEPTED,
                    EventSource.USER,
                    {
                        "obligation_id": obligation_id,
                        "reason": (
                            "operator opted in to deliver with an unverified gap"
                        ),
                        "accepted_by": "user",
                        "obligation_kind": obligation.kind.value,
                    },
                    step_id=self._current_step_id(),
                )
            self._append(
                EventKind.DEBT_OPENED,
                EventSource.GATE,
                {
                    "obligation_id": obligation_id,
                    "kind": obligation.kind.value,
                    "target": obligation.target,
                    "reason": debt.reason,
                    "stage": verdict.stage.value,
                    "attempts": debt.attempts,
                },
                step_id=self._current_step_id(),
            )
            # Self-evolution (design/self-evolution.md, slice 2): a debt the
            # harness itself opened is a lesson source.  The proposal is
            # written here, visible in this task's ledger; storage and
            # application are later slices.
            self._propose_lesson(
                "debt_opened",
                {
                    "kind": obligation.kind.value,
                    "target": obligation.target,
                    "reason": debt.reason,
                },
            )

    def _propose_lesson(self, event_kind: str, payload: dict[str, Any]) -> None:
        """Write a LESSON_PROPOSED event from a harness-judged failure (slice 2).

        The proposal is a deterministic template over the payload; it never
        invents a reason and never uses the model's prose.  Best-effort: a
        broken proposer must not fail the task.
        """

        try:
            from xenon.kernel.lesson_proposer import propose_lesson

            proposal = propose_lesson(
                event_kind,
                payload,
                source_task=(
                    self._spec.task_id if self._spec is not None else ""
                ),
            )
        except Exception:  # noqa: BLE001 — telemetry never fails a task
            return
        if proposal is None:
            return
        self._append(
            EventKind.LESSON_PROPOSED,
            EventSource.GATE,
            {
                "kind": proposal.kind,
                "trigger_event": proposal.trigger_event,
                "fingerprint": proposal.fingerprint,
                "action_text": proposal.action_text,
                "counterfactual": proposal.counterfactual,
                "source_task": proposal.source_task,
            },
        )

    def _find_obligation(
        self, obligation_id: str, verdict: Verdict
    ) -> Obligation | None:
        if self._spec is not None:
            for obligation in self._spec.obligations:
                if obligation.id == obligation_id:
                    return self._baseline_obligation(obligation)
        for step in self._plan_steps:
            for obligation in step.expects:
                if obligation.id == obligation_id:
                    return self._baseline_obligation(obligation)
        return None

    def _has_workspace_evidence(self) -> bool:
        """Did a recorded probe answer a declared "no stray changes" obligation?

        A verification-only task ("confirm the worktree is clean") runs no
        tool, so the "at least one successful receipt" rule would block it even
        though the harness has exactly the evidence the task asked for.  The
        probe only counts when the task actually declared that obligation —
        otherwise it would weaken the rule for ordinary tasks.
        """

        if not self._probed_signature:
            return False
        declared = list(getattr(self._spec, "obligations", ()) or ())
        for step in self._plan_steps:
            declared.extend(step.expects)
        return any(
            obligation.kind is PropositionKind.NO_STRAY_CHANGES
            for obligation in declared
        )

    def _has_successful_receipt(self) -> bool:
        for event in self._ledger.read(EventKind.TOOL_RECEIPT):
            if event.seq <= self._run_start_seq:
                continue
            payload = event.payload.get("receipt")
            if isinstance(payload, dict) and payload.get("status") == "ok":
                return True
        return False

    def _answered_without_actions(self) -> bool:
        """The plan came back empty and the model still answered.

        Only an explicit empty plan means "this task needs no tool".  A run
        that proposed steps but executed nothing is a failed delivery and must
        stay one; the difference is recorded, not inferred from the answer.
        """

        if any(
            event.seq > self._run_start_seq
            for event in self._ledger.read(EventKind.TOOL_RECEIPT)
        ):
            return False
        if not self._last_answer.strip():
            return False
        proposed = [
            event
            for event in self._ledger.read(EventKind.PLAN_PROPOSED)
            if event.seq > self._run_start_seq
        ]
        if not proposed:
            return False
        # Empty, or reasoning-only: both are the planner saying "no tool work".
        # A step may still carry a capability (models like to attach `read`
        # with no target); what matters is that it declares nothing to change
        # and expects nothing to be proven.
        for event in proposed:
            for step in (event.payload.get("plan") or {}).get("steps") or []:
                if str(step.get("kind") or "") not in {"think", ""}:
                    return False
                if step.get("expects"):
                    return False
        return True

    def _baseline_claim(self, claim: Claim) -> Claim:
        if claim.since_seq:
            return claim
        return Claim(
            id=claim.id,
            kind=claim.kind,
            target=claim.target,
            expect=dict(claim.expect),
            source=claim.source,
            since_seq=self._run_start_seq,
        )

    def _last_commit_sha(self) -> str | None:
        for event in reversed(self._ledger.read(EventKind.TOOL_RECEIPT)):
            if event.seq <= self._run_start_seq:
                continue
            payload = event.payload.get("receipt")
            if not isinstance(payload, dict):
                continue
            receipt = receipt_from_payload(payload)
            for effect in receipt.effects:
                if isinstance(effect, VcsCommit):
                    return effect.sha
        return None

    def _append(
        self,
        kind: EventKind,
        source: EventSource,
        payload: dict[str, Any],
        *,
        step_id: str | None = None,
    ) -> Any:
        event = self._ledger.append(
            kind, source, payload, step_id=step_id
        )
        if self._sink is not None:
            try:
                self._sink.emit(event)
            except Exception:  # noqa: BLE001 — UI failure must not corrupt state
                logger.warning("event sink failed", exc_info=True)
        return event

    def _transition(self, target: TaskPhase, reason: str = "") -> None:
        source = self._phase
        if source is target:
            # Re-stating the current phase is redundant, not illegal.  Raising
            # here turned a harmless no-op into a crash of the whole run
            # (observed on a real run: ``running -> running`` after a released
            # step that had no violation to repair).
            return
        assert_transition(source, target)
        self._append(
            EventKind.STATE_TRANSITION,
            EventSource.RUNTIME,
            {
                "from": source.value,
                "to": target.value,
                "reason": reason,
            },
        )
        self._phase = target

    @staticmethod
    def _compute_deadline(spec: TaskSpec) -> float | None:
        if spec.deadline is not None:
            return spec.deadline
        if spec.budget.max_wall_seconds is not None:
            return time.time() + spec.budget.max_wall_seconds
        return None


def _readonly_view(ledger: Ledger) -> Any:
    """Prefer the concrete read-only wrapper; tests may pass a fake ledger."""

    try:
        from xenon.ledger.store import ReadOnlyLedger
    except ImportError:  # pragma: no cover — ledger is part of the package
        return ledger
    return ReadOnlyLedger(ledger)  # type: ignore[arg-type]


def _is_commit_step(step: StepContract) -> bool:
    return step.kind is StepKind.COMMIT or any(
        item.kind is PropositionKind.COMMIT_CREATED
        for item in step.expects
    )


def _assert_acyclic(steps: Sequence[StepContract]) -> None:
    resolved: set[int] = set()
    pending = list(steps)
    while pending:
        ready = [
            step
            for step in pending
            if all(item in resolved for item in step.depends_on)
        ]
        if not ready:
            raise ValueError("plan dependencies contain a cycle")
        for step in ready:
            resolved.add(step.id)
            pending.remove(step)


def _serialize_step(step: StepContract, *, task_id: str = "") -> dict[str, Any]:
    intent = step.intent
    return {
        "step_id": step.id,
        "task": task_id,
        "kind": step.kind.value,
        "targets": list(step.targets),
        "depends_on": list(step.depends_on),
        "prefer": step.prefer,
        "intent": (
            None
            if intent is None
            else {
                "capability": intent.capability.value,
                "params": dict(intent.params),
                "targets": list(intent.targets),
                "prefer": intent.prefer,
            }
        ),
        "expects": [
            {
                "id": item.id,
                "kind": item.kind.value,
                "target": item.target,
                "expect": dict(item.expect),
                "since_seq": item.since_seq,
            }
            for item in step.expects
        ],
    }


def _plan_payload(
    plan: Any, *, steps: Sequence[StepContract] | None = None
) -> dict[str, Any]:
    selected = tuple(steps) if steps is not None else tuple(
        getattr(plan, "steps", ())
    )
    return {
        "analysis": getattr(plan, "analysis", ""),
        "steps": [_serialize_step(step) for step in selected],
    }


def _latest_verdict_map(
    verdicts: Sequence[Verdict],
) -> dict[str, Verdict]:
    """One verdict per obligation; later verdicts replace earlier ones."""

    latest: dict[str, Verdict] = {}
    for verdict in verdicts:
        latest[verdict.obligation_id] = verdict
    return latest


def _latest_verdicts(verdicts: Sequence[Verdict]) -> tuple[Verdict, ...]:
    return tuple(_latest_verdict_map(verdicts).values())


def _resolution_payload(resolution: Resolution) -> dict[str, Any]:
    return {
        "claim_id": resolution.claim_id,
        "status": resolution.status.value,
        "fact_ids": list(resolution.fact_ids),
        "reason": resolution.reason,
        "candidates": list(resolution.candidates),
    }


def _claim_payload(claim: Claim) -> dict[str, Any]:
    return {
        "claim_id": claim.id,
        "kind": claim.kind.value,
        "target": claim.target,
        "expect": dict(claim.expect),
        "source": claim.source.value,
        "since_seq": claim.since_seq,
    }


def _verdict_payload(verdict: Verdict) -> dict[str, Any]:
    return {
        "obligation_id": verdict.obligation_id,
        "stage": verdict.stage.value,
        "status": verdict.status.value,
        "fact_ids": list(verdict.fact_ids),
        "reason": verdict.reason,
    }


def _coverage_payload(rows: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "obligation_id": row.obligation_id,
            "kind": row.kind.value,
            "target": row.target,
            "status": row.status.value,
            "fact_ids": list(row.fact_ids),
            "reason": row.reason,
            "next_action": row.next_action,
            "source": row.source,
        }
        for row in rows
    ]


def _gaps_payload(gaps: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "obligation_id": gap.obligation_id,
            "reason": gap.reason,
            "next_action": gap.next_action,
        }
        for gap in gaps
    ]
