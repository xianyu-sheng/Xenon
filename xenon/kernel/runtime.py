"""Single-loop runtime (#196).

The runtime is the only writer.  Strategies propose actions; policies veto;
the gateway executes; the controller verifies; the ledger records.  There is no
second state machine and no direct tool dispatch in this module.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Sequence

from xenon.kernel.contracts import (
    Abort,
    Action,
    AskUser,
    BindingStatus,
    CallTool,
    Claim,
    ControlDecisionKind,
    Debt,
    EvidenceStatus,
    Finish,
    Obligation,
    ObligationSource,
    PolicyDecisionKind,
    PropositionKind,
    RequestVerification,
    StepContract,
    SubmitPlan,
    TaskMode,
    TaskPhase,
    TaskResult,
    TaskSpec,
    Think,
    UpdatePlan,
    VcsCommit,
    Verdict,
    VerdictStatus,
)
from xenon.kernel.phases import assert_transition
from xenon.kernel.protocols import (
    EventSink,
    Ledger,
    Policy,
    Strategy,
    ToolCatalog,
    ToolGateway,
    VerificationController,
)
from xenon.kernel.view import build_model_view
from xenon.ledger.events import (
    EventKind,
    EventSource,
    receipt_event_payload,
    receipt_from_payload,
)

logger = logging.getLogger(__name__)


class _RuntimeServices:
    """Small gateway-facing surface; never exposes the mutable ledger."""

    def __init__(self, runtime: "LoopRuntime") -> None:
        self._runtime = runtime

    @property
    def workspace(self) -> str:
        return self._runtime._spec.workspace if self._runtime._spec else ""

    @property
    def current_step_id(self) -> str | None:
        return self._runtime._current_step_id()

    def is_cancelled(self) -> bool:
        return self._runtime._cancelled


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
    ) -> None:
        self._catalog = catalog
        self._gateway = gateway
        self._controller = controller
        self._ledger = ledger
        self._readonly = _readonly_view(ledger)
        self._sink = sink
        self._policies = tuple(policies)
        self._services = _RuntimeServices(self)

        self._spec: TaskSpec | None = None
        self._phase = TaskPhase.CREATED
        self._steps = 0
        self._tool_calls = 0
        self._cancelled = False
        self._deadline: float | None = None
        self._plan: Any = None
        self._active_step: StepContract | None = None
        self._verdicts: list[Verdict] = []
        self._debt: list[Any] = []
        self._run_start_seq = 0

    # ── public surface ─────────────────────────────────────────────────

    def request_cancel(self) -> None:
        self._cancelled = True

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
        self._run_start_seq = 0
        self._plan = None
        self._active_step = None
        self._deadline = self._compute_deadline(spec)

        self._append(
            EventKind.TASK_STARTED,
            EventSource.RUNTIME,
            {
                "objective": spec.objective,
                "workspace": spec.workspace,
                "mode": spec.mode.value,
                "constraints": list(spec.constraints),
            },
        )
        self._run_start_seq = self._ledger.watermark()
        self._transition(TaskPhase.INTAKE, "task accepted")
        self._append(
            EventKind.INTAKE_RECORDED,
            EventSource.RUNTIME,
            {"mode": spec.mode.value, "constraints": list(spec.constraints)},
        )
        self._transition(TaskPhase.RUNNING, "intake complete")

        while True:
            limited = self._check_limits()
            if limited is not None:
                return limited

            view = build_model_view(
                spec=spec,
                catalog=self._catalog,
                ledger=self._readonly,
                debt=tuple(self._debt),
                budget=spec.budget,
                phase=self._phase,
            )
            self._transition(TaskPhase.THINKING, "propose action")
            try:
                action = strategy.propose(view)
            except Exception:
                self._transition(TaskPhase.FAILED, "strategy error")
                raise
            self._transition(TaskPhase.RUNNING, "action proposed")
            self._steps += 1

            veto = self._check_policies(view, action)
            if veto is not None:
                return veto

            if isinstance(action, Think):
                self._append(
                    EventKind.THOUGHT,
                    EventSource.LLM,
                    {"text": action.text},
                )
                continue

            if isinstance(action, SubmitPlan):
                self._accept_plan(action.plan)
                continue

            if isinstance(action, UpdatePlan):
                self._apply_plan(action.plan, updated=True)
                continue

            if isinstance(action, RequestVerification):
                self._append(
                    EventKind.CLAIM_PROPOSED,
                    EventSource.LLM,
                    _claim_payload(self._baseline_claim(action.claim)),
                )
                continue

            if isinstance(action, CallTool):
                self._call_tool(action)
                continue

            if isinstance(action, Finish):
                result = self._finish(action)
                if result is not None:
                    return result
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

    def _accept_plan(self, plan: Any) -> None:
        self._apply_plan(plan, updated=False)

    def _apply_plan(self, plan: Any, *, updated: bool) -> None:
        payload = {"plan": _plan_payload(plan)}
        if updated:
            self._append(EventKind.PLAN_UPDATED, EventSource.RUNTIME, payload)
        else:
            self._append(EventKind.PLAN_PROPOSED, EventSource.LLM, payload)
            self._append(EventKind.PLAN_ACCEPTED, EventSource.RUNTIME, payload)
        self._plan = plan
        steps = getattr(plan, "steps", ())
        self._active_step = steps[0] if steps else None

    def _call_tool(self, action: CallTool) -> None:
        binding = self._catalog.bind(action.intent)
        if binding.status is not BindingStatus.BOUND or binding.call is None:
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
            self._transition(TaskPhase.REPLANNING, "no deterministic binding")
            self._transition(TaskPhase.RUNNING, "replan after binding failure")
            return

        self._transition(TaskPhase.TOOL_CALL, binding.call.spec_name)
        receipt = self._gateway.execute(binding.call, self._services)
        self._append(
            EventKind.TOOL_RECEIPT,
            EventSource.TOOL,
            receipt_event_payload(receipt),
            step_id=self._current_step_id(),
        )
        self._tool_calls += 1
        self._transition(TaskPhase.RECONCILING, "receipt recorded")

        verdict = self._controller.reconcile(self._active_step, receipt)
        self._verdicts.append(verdict)
        self._append(
            EventKind.STEP_VERDICT,
            EventSource.GATE,
            _verdict_payload(verdict),
            step_id=self._current_step_id(),
        )
        if verdict.status is VerdictStatus.VIOLATED:
            self._transition(TaskPhase.REPAIRING, verdict.reason or "violated")
        self._transition(TaskPhase.RUNNING, "next action")

    def _finish(self, action: Finish) -> TaskResult | None:
        claims = tuple(self._baseline_claim(item) for item in action.claims)
        for claim in claims:
            self._append(
                EventKind.CLAIM_PROPOSED,
                EventSource.LLM,
                _claim_payload(claim),
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
        self._open_debt_from(self._verdicts)
        self._append(
            EventKind.AUDIT_REPORTED,
            EventSource.GATE,
            {
                "verdicts": [_verdict_payload(item) for item in checks],
                "claims": [_claim_payload(item) for item in claims],
                "debt": [item.obligation.id for item in self._debt],
            },
        )

        decision = self._controller.decide(
            tuple(self._verdicts), tuple(self._debt)
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

        if decision.kind is ControlDecisionKind.DELIVER:
            self._transition(TaskPhase.DELIVERING, decision.reason)
            self._append(
                EventKind.DELIVERED,
                EventSource.RUNTIME,
                {
                    "answer": action.answer,
                    "claims": [_claim_payload(item) for item in claims],
                    "verdicts": [_verdict_payload(item) for item in checks],
                },
            )
            self._transition(TaskPhase.DELIVERED, "accepted")
            sha = self._last_commit_sha()
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
            )

        if decision.kind is ControlDecisionKind.ABORT:
            self._transition(TaskPhase.ABORTED, decision.reason or "aborted")
            return self._result(
                accepted=False,
                evidence=EvidenceStatus.UNVERIFIED,
            )

        self._transition(TaskPhase.BLOCKED, decision.reason or "blocked")
        return self._result(
            accepted=False,
            evidence=EvidenceStatus.UNVERIFIED,
        )

    # ── policies and limits ────────────────────────────────────────────

    def _check_policies(self, view: Any, action: Action) -> TaskResult | None:
        for policy in self._policies:
            decision = policy.check(view, action)
            if decision.kind is PolicyDecisionKind.ALLOW:
                continue
            self._append(
                EventKind.POLICY_DECISION,
                EventSource.GATE,
                {
                    "kind": decision.kind.value,
                    "reason": decision.reason,
                    "scope": list(decision.scope),
                },
            )
            self._transition(
                TaskPhase.BLOCKED, decision.reason or "policy blocked"
            )
            return self._result(
                accepted=False,
                evidence=EvidenceStatus.ABSTAINED,
            )
        return None

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
            self._transition(TaskPhase.BLOCKED, "step budget exhausted")
            return self._result(
                accepted=False, evidence=EvidenceStatus.UNVERIFIED
            )
        if self._deadline is not None and time.time() > self._deadline:
            self._transition(TaskPhase.BLOCKED, "time budget exhausted")
            return self._result(
                accepted=False, evidence=EvidenceStatus.UNVERIFIED
            )
        return None

    # ── results and persistence ────────────────────────────────────────

    def _result(
        self,
        *,
        accepted: bool,
        evidence: EvidenceStatus,
        answer: str = "",
        claims: Sequence[Claim] = (),
        finalize_sha: str | None = None,
    ) -> TaskResult:
        assert self._spec is not None
        return TaskResult(
            task_id=self._spec.task_id,
            accepted=accepted,
            phase=self._phase,
            answer=answer,
            claims=tuple(claims),
            verdicts=tuple(self._verdicts),
            debt=tuple(self._debt),
            finalize_sha=finalize_sha,
            evidence_status=evidence,
            metrics={
                "steps": float(self._steps),
                "tool_calls": float(self._tool_calls),
                "events": float(self._ledger.watermark()),
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
            return EvidenceStatus.VERIFIED
        return EvidenceStatus.PARTIAL

    def _current_step_id(self) -> str | None:
        if self._active_step is None:
            return None
        return str(self._active_step.id)

    def _open_debt_from(self, verdicts: Sequence[Verdict]) -> None:
        existing = {item.obligation.id for item in self._debt}
        for verdict in verdicts:
            if verdict.status is VerdictStatus.SATISFIED:
                continue
            obligation_id = verdict.obligation_id or (
                f"obligation-{len(self._debt) + 1}"
            )
            if obligation_id in existing:
                continue
            obligation = Obligation(
                id=obligation_id,
                kind=PropositionKind.REQUIREMENT_MET,
                source=ObligationSource.USER,
                required=True,
                since_seq=self._run_start_seq,
            )
            debt = Debt(
                obligation=obligation,
                reason=verdict.reason or verdict.status.value,
            )
            self._debt.append(debt)
            existing.add(obligation_id)
            self._append(
                EventKind.DEBT_OPENED,
                EventSource.GATE,
                {
                    "obligation_id": obligation_id,
                    "reason": debt.reason,
                    "stage": verdict.stage.value,
                },
                step_id=self._current_step_id(),
            )

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


def _plan_payload(plan: Any) -> dict[str, Any]:
    steps = getattr(plan, "steps", ())
    return {
        "analysis": getattr(plan, "analysis", ""),
        "steps": [
            {
                "id": getattr(step, "id", 0),
                "kind": getattr(getattr(step, "kind", None), "value", ""),
                "targets": list(getattr(step, "targets", ())),
                "depends_on": list(getattr(step, "depends_on", ())),
                "expects": [
                    getattr(item, "id", "")
                    for item in getattr(step, "expects", ())
                ],
            }
            for step in steps
        ],
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
