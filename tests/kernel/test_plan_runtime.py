"""Plan step lifecycle and Finish fail-closed tests (#198)."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from tests.kernel.test_north_star import (
    FakeCatalog,
    FakeGateway,
    ScriptedStrategy,
    _calc_spec,
)
from xenon.kernel.contracts import (
    Budget,
    CallTool,
    Capability,
    Claim,
    ClaimSource,
    EffectKind,
    Finish,
    Obligation,
    ObligationSource,
    PlanProposal,
    PropositionKind,
    ReceiptStatus,
    ResolutionStatus,
    StepContract,
    StepKind,
    SubmitPlan,
    TaskMode,
    TaskPhase,
    TaskSpec,
    TestRun,
    ToolIntent,
    ToolReceipt,
)
from xenon.kernel.controller import VerificationController
from xenon.kernel.runtime import LoopRuntime
from xenon.kernel.strategies import LoopProfile, LoopStrategy
from xenon.ledger.events import EventKind, EventSource, canonical_json
from xenon.ledger.store import Ledger


def _follow(plan: PlanProposal) -> LoopStrategy:
    """Plan plus deterministic step execution (the old PlanFirst shape)."""

    return LoopStrategy(
        LoopProfile(plan=True, step_scope=True, follow_plan=True),
        actor=lambda view: Finish("completed"),
        planner=lambda view: plan,
    )


_PROFILE_FIELDS = frozenset(
    {"step_calls", "step_reflect", "reflect_rounds", "reflect_calls"}
)


def _react(plan: PlanProposal, actor, **kwargs) -> LoopStrategy:
    """Plan plus a bounded per-step sub-loop (the old PlanReact shape)."""

    planner = plan if callable(plan) else (lambda view: plan)
    profile_keys = {k: v for k, v in kwargs.items() if k in _PROFILE_FIELDS}
    providers = {k: v for k, v in kwargs.items() if k not in _PROFILE_FIELDS}
    plan_reviewer = providers.pop("plan_reviewer", None)
    step_reviewer = providers.pop("step_reviewer", None)
    if plan_reviewer is not None:
        profile_keys["plan_review"] = True
    if step_reviewer is not None:
        profile_keys["step_reflect"] = True
    return LoopStrategy(
        LoopProfile(plan=True, step_scope=True, **profile_keys),
        actor=lambda view: Finish("completed"),
        step_actor=actor,
        planner=planner,
        plan_reviewer=plan_reviewer,
        step_reviewer=step_reviewer,
        **providers,
    )


def _runtime(ledger: Ledger) -> LoopRuntime:
    return LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
    )


class CountingGateway(FakeGateway):
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, call, services) -> ToolReceipt:
        self.calls += 1
        return super().execute(call, services)


class FakeFinalizer:
    def __init__(self) -> None:
        self.calls = 0

    def finalize(self, **kwargs):
        self.calls += 1
        del kwargs
        return type(
            "Outcome",
            (),
            {
                "accepted": True,
                "created": True,
                "commit_sha": "def5678",
                "files": ("calc/add.py",),
                "reason": "",
            },
        )()


class AlwaysFailingGateway:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, call, services) -> ToolReceipt:
        self.calls += 1
        effect = TestRun(
            target="tests/test_calc.py",
            tests_run=1,
            passed=0,
            failed=1,
            exit_code=1,
        )
        return ToolReceipt(
            receipt_id=f"always-fail-{self.calls}",
            call_id=f"always-fail-call-{self.calls}",
            task_id="task-1",
            tool=call.spec_name,
            status=ReceiptStatus.OK,
            effects=(effect,),
            declared_effects=(EffectKind.TEST_RUN,),
            observed_effects=(EffectKind.TEST_RUN,),
        )


class FlakyGateway:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, call, services) -> ToolReceipt:
        self.calls += 1
        passing = self.calls > 1
        effect = TestRun(
            target="tests/test_calc.py",
            tests_run=1,
            passed=1 if passing else 0,
            failed=0 if passing else 1,
            exit_code=0 if passing else 1,
        )
        return ToolReceipt(
            receipt_id=f"flaky-{self.calls}",
            call_id=f"flaky-call-{self.calls}",
            task_id="task-1",
            tool=call.spec_name,
            status=ReceiptStatus.OK,
            effects=(effect,),
            declared_effects=(EffectKind.TEST_RUN,),
            observed_effects=(EffectKind.TEST_RUN,),
        )


def _plan() -> PlanProposal:
    return PlanProposal(
        analysis="edit, verify, record",
        steps=(
            StepContract(
                id=1,
                kind=StepKind.EDIT,
                intent=ToolIntent(capability=Capability.EDIT),
                targets=("calc/add.py",),
                expects=(
                    Obligation(
                        id="edit-1",
                        kind=PropositionKind.FILE_CHANGED,
                        target="calc/add.py",
                        source=ObligationSource.PLAN,
                    ),
                ),
            ),
            StepContract(
                id=2,
                kind=StepKind.VERIFY,
                intent=ToolIntent(capability=Capability.VERIFY),
                targets=("tests/test_calc.py",),
                expects=(
                    Obligation(
                        id="test-1",
                        kind=PropositionKind.TEST_PASSED,
                        target="tests/test_calc.py",
                        source=ObligationSource.PLAN,
                    ),
                ),
            ),
            StepContract(
                id=3,
                kind=StepKind.COMMIT,
                intent=ToolIntent(capability=Capability.VCS),
                expects=(
                    Obligation(
                        id="commit-1",
                        kind=PropositionKind.COMMIT_CREATED,
                        source=ObligationSource.PLAN,
                    ),
                ),
            ),
        ),
    )


def _verify_plan() -> PlanProposal:
    return PlanProposal(
        steps=(
            StepContract(
                id=1,
                kind=StepKind.VERIFY,
                intent=ToolIntent(capability=Capability.VERIFY),
                expects=(
                    Obligation(
                        id="bounded-test",
                        kind=PropositionKind.TEST_PASSED,
                        target="tests/test_calc.py",
                        source=ObligationSource.PLAN,
                    ),
                ),
            ),
        )
    )


def test_repair_budget_blocks_after_configured_attempts() -> None:
    ledger = Ledger.in_memory("task-1")
    gateway = AlwaysFailingGateway()
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=gateway,
        controller=VerificationController(),
        ledger=ledger,
    )
    spec = TaskSpec(
        task_id="task-1",
        objective="fix",
        workspace=".",
        budget=Budget(max_steps=20, max_repair_attempts=2),
    )

    result = runtime.run(spec, _follow(_verify_plan()))

    assert result.accepted is False
    assert result.phase.value == "blocked"
    assert gateway.calls == 3
    assert result.debt[0].attempts == 3
    assert any(
        event.kind is EventKind.POLICY_DECISION
        and "repair budget exhausted" in str(event.payload.get("reason"))
        for event in ledger.read()
    )
    assert result.metrics["repair_attempts"] == 3.0


def test_zero_repair_budget_blocks_on_first_failure() -> None:
    ledger = Ledger.in_memory("task-1")
    gateway = AlwaysFailingGateway()
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=gateway,
        controller=VerificationController(),
        ledger=ledger,
    )
    spec = TaskSpec(
        task_id="task-1",
        objective="fix",
        workspace=".",
        budget=Budget(max_steps=20, max_repair_attempts=0),
    )

    result = runtime.run(spec, _follow(_verify_plan()))

    assert result.accepted is False
    assert gateway.calls == 1


def test_replan_budget_blocks_on_invalid_plan() -> None:
    ledger = Ledger.in_memory("task-1")
    first = StepContract(
        id=1,
        kind=StepKind.EDIT,
        intent=ToolIntent(capability=Capability.EDIT),
        depends_on=(2,),
    )
    second = StepContract(
        id=2,
        kind=StepKind.VERIFY,
        intent=ToolIntent(capability=Capability.VERIFY),
        depends_on=(1,),
    )
    spec = TaskSpec(
        task_id="task-1",
        objective="fix",
        workspace=".",
        budget=Budget(max_steps=20, max_replans=0),
    )
    result = _runtime(ledger).run(
        spec,
        ScriptedStrategy(
            [SubmitPlan(PlanProposal(steps=(first, second))), Finish("done")]
        ),
    )

    assert result.accepted is False
    assert result.phase.value == "blocked"
    assert any(
        event.kind is EventKind.POLICY_DECISION
        and "replan budget exhausted" in str(event.payload.get("reason"))
        for event in ledger.read()
    )


def test_failed_step_can_be_repaired_and_closes_debt() -> None:
    ledger = Ledger.in_memory("task-1")
    plan = PlanProposal(
        steps=(
            StepContract(
                id=1,
                kind=StepKind.VERIFY,
                intent=ToolIntent(capability=Capability.VERIFY),
                expects=(
                    Obligation(
                        id="test-retry",
                        kind=PropositionKind.TEST_PASSED,
                        target="tests/test_calc.py",
                        source=ObligationSource.PLAN,
                    ),
                ),
            ),
        )
    )
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FlakyGateway(),
        controller=VerificationController(),
        ledger=ledger,
    )

    result = runtime.run(_calc_spec(), _follow(plan))

    assert result.accepted is True
    assert not result.debt
    assert any(event.kind is EventKind.DEBT_CLOSED for event in ledger.read())


def _two_step_plan() -> PlanProposal:
    return PlanProposal(
        steps=(
            StepContract(
                id=1,
                kind=StepKind.INSPECT,
                intent=ToolIntent(capability=Capability.READ),
            ),
            StepContract(
                id=2,
                kind=StepKind.EDIT,
                intent=ToolIntent(capability=Capability.EDIT),
                depends_on=(1,),
            ),
        )
    )


def test_plan_react_records_per_step_work_and_closes_in_order() -> None:
    ledger = Ledger.in_memory("task-1")
    seen: list[int] = []

    def actor(view, step, calls):
        seen.append(step.id)
        return CallTool(step.intent or ToolIntent(capability=Capability.READ))

    result = _runtime(ledger).run(
        _calc_spec(), _react(_two_step_plan(), actor)
    )

    assert result.accepted is True
    assert seen == [1, 2], "每次只推进一个激活步骤"
    started = [
        event.payload["step_id"]
        for event in ledger.read(EventKind.STEP_STARTED)
    ]
    assert started == [1, 2]
    assert [event.kind for event in ledger.read(EventKind.STEP_CLOSED)] == [
        EventKind.STEP_CLOSED,
        EventKind.STEP_CLOSED,
    ]
    receipts = ledger.read(EventKind.TOOL_RECEIPT)
    assert len(receipts) == 2, "每一步都留下回执"


def test_plan_react_step_budget_is_visible_in_the_ledger() -> None:
    ledger = Ledger.in_memory("task-1")

    result = _runtime(ledger).run(
        _calc_spec(),
        _react(
            lambda view: PlanProposal(
                steps=(
                    StepContract(
                        id=1,
                        kind=StepKind.INSPECT,
                        intent=ToolIntent(capability=Capability.READ),
                        expects=(
                            Obligation(
                                id="impossible",
                                kind=PropositionKind.FILE_CHANGED,
                                target="never.py",
                                source=ObligationSource.PLAN,
                            ),
                        ),
                    ),
                )
            ),
            lambda view, step, calls: CallTool(
                ToolIntent(capability=Capability.READ)
            ),
            step_calls=2,
        ),
    )

    explored = ledger.read(EventKind.STEP_EXPLORED)
    assert explored, "步内探索必须有账本记录"
    assert explored[0].payload["decision"] == "budget_exhausted"
    assert explored[0].payload["calls"] == 2
    assert result.accepted is False, "步内预算耗尽不能变成交付"
    assert result.debt


def test_plan_react_never_closes_a_step_without_its_expectation() -> None:
    ledger = Ledger.in_memory("task-1")

    result = _runtime(ledger).run(
        _calc_spec(),
        _react(
            lambda view: PlanProposal(
                steps=(
                    StepContract(
                        id=1,
                        kind=StepKind.EDIT,
                        intent=ToolIntent(capability=Capability.READ),
                        expects=(
                            Obligation(
                                id="needs-edit",
                                kind=PropositionKind.FILE_CHANGED,
                                target="calc/add.py",
                                source=ObligationSource.PLAN,
                            ),
                        ),
                    ),
                )
            ),
            lambda view, step, calls: Finish("step looks done"),
        ),
    )

    assert result.accepted is False
    assert result.debt




def test_step_verification_reaches_the_step_reviewer() -> None:
    """Reflection reads the deterministic verdicts; it never produces them."""

    ledger = Ledger.in_memory("task-1")
    seen: list[tuple[str, str]] = []
    plan = PlanProposal(
        steps=(
            StepContract(
                id=1,
                kind=StepKind.VERIFY,
                intent=ToolIntent(capability=Capability.VERIFY),
                expects=(
                    Obligation(
                        id="needs-tests",
                        kind=PropositionKind.TEST_PASSED,
                        target="tests/test_calc.py",
                        source=ObligationSource.PLAN,
                    ),
                ),
            ),
        )
    )

    def reviewer(view, step, action):
        for verdict in view.step_verdicts:
            seen.append((verdict.obligation_id, verdict.status.value))
        return None

    _runtime(ledger).run(
        _calc_spec(),
        _react(
            plan,
            lambda view, step, calls: (
                CallTool(ToolIntent(capability=Capability.READ))
                if calls == 1
                else Finish("step done")
            ),
            step_reviewer=reviewer,
        ),
    )

    assert seen, "步骤级校验必须喂给反思"
    assert ("needs-tests", "violated") in seen


def test_every_closed_step_records_one_step_verified_event() -> None:
    ledger = Ledger.in_memory("task-1")

    _runtime(ledger).run(
        _calc_spec(),
        _react(
            _two_step_plan(),
            lambda view, step, calls: CallTool(
                step.intent or ToolIntent(capability=Capability.READ)
            ),
        ),
    )

    verified = ledger.read(EventKind.STEP_VERIFIED)
    assert [event.payload["step_id"] for event in verified] == [1, 2]
    assert all(event.payload["status"] == "satisfied" for event in verified)
    # individual verdicts are still there: the summary does not replace them
    assert ledger.read(EventKind.STEP_VERDICT)


def test_plan_review_added_step_actually_runs() -> None:
    ledger = Ledger.in_memory("task-1")
    reviewed = PlanProposal(
        steps=_two_step_plan().steps
        + (
            StepContract(
                id=3,
                kind=StepKind.VERIFY,
                intent=ToolIntent(capability=Capability.VERIFY),
            ),
        )
    )

    result = _runtime(ledger).run(
        _calc_spec(),
        _react(
            lambda view: _two_step_plan(),
            lambda view, step, calls: CallTool(
                step.intent or ToolIntent(capability=Capability.READ)
            ),
            plan_reviewer=lambda view, plan: reviewed,
        ),
    )

    assert result.accepted is True
    started = [event.payload["step_id"] for event in ledger.read(EventKind.STEP_STARTED)]
    assert started == [1, 2, 3], "计划审查补进来的步骤必须真的执行"
    assert len(ledger.read(EventKind.TOOL_RECEIPT)) == 3


def test_plan_review_decision_is_recorded_as_plan_reviewed() -> None:
    ledger = Ledger.in_memory("task-1")

    _runtime(ledger).run(
        _calc_spec(),
        _react(
            lambda view: _two_step_plan(),
            lambda view, step, calls: CallTool(
                step.intent or ToolIntent(capability=Capability.READ)
            ),
            plan_reviewer=lambda view, plan: None,
        ),
    )

    reviewed = ledger.read(EventKind.PLAN_REVIEWED)
    assert len(reviewed) == 1
    assert reviewed[0].payload["decision"] == "accepted"
    assert reviewed[0].payload["added_steps"] == 0
    assert not ledger.read(EventKind.REFLECTION_PROPOSED)


def test_step_review_corrects_a_failing_step_before_release() -> None:
    ledger = Ledger.in_memory("task-1")
    # The step must run pytest.  Its first action is a read, so the step stays
    # open (violated) — that is exactly when a review can still fix it.
    plan = PlanProposal(
        steps=(
            StepContract(
                id=1,
                kind=StepKind.VERIFY,
                intent=ToolIntent(capability=Capability.VERIFY),
                expects=(
                    Obligation(
                        id="needs-tests",
                        kind=PropositionKind.TEST_PASSED,
                        target="tests/test_calc.py",
                        source=ObligationSource.PLAN,
                    ),
                ),
            ),
        )
    )

    result = _runtime(ledger).run(
        _calc_spec(),
        _react(
            lambda view: plan,
            lambda view, step, calls: (
                CallTool(ToolIntent(capability=Capability.READ))
                if calls == 1
                else Finish("step done")
            ),
            step_reviewer=lambda view, step, action: CallTool(
                ToolIntent(capability=Capability.VERIFY)
            ),
        ),
    )

    assert result.accepted is True
    receipts = [
        event.payload["receipt"]["tool"] for event in ledger.read(EventKind.TOOL_RECEIPT)
    ]
    assert receipts == ["inspect", "check"], "审查在同一步骤内补上真正的验证"
    started = [
        event.payload["step_id"] for event in ledger.read(EventKind.STEP_STARTED)
    ]
    assert started == [1], "修正发生在同一步骤，没有越步"
    assert not result.debt


def test_step_review_cannot_bypass_an_unmet_expectation() -> None:
    ledger = Ledger.in_memory("task-1")
    plan = PlanProposal(
        steps=(
            StepContract(
                id=1,
                kind=StepKind.EDIT,
                intent=ToolIntent(capability=Capability.READ),
                expects=(
                    Obligation(
                        id="needs-edit",
                        kind=PropositionKind.FILE_CHANGED,
                        target="calc/add.py",
                        source=ObligationSource.PLAN,
                    ),
                ),
            ),
        )
    )

    result = _runtime(ledger).run(
        _calc_spec(),
        _react(
            lambda view: plan,
            lambda view, step, calls: Finish("looks done"),
            step_reviewer=lambda view, step, action: None,
        ),
    )

    assert result.accepted is False
    assert result.debt




def test_invalid_plan_is_rejected_without_crashing() -> None:
    ledger = Ledger.in_memory("task-1")
    bad = PlanProposal(
        steps=(
            StepContract(
                id=1,
                kind=StepKind.EDIT,
                intent=ToolIntent(capability=Capability.EDIT),
                depends_on=(1,),
            ),
        )
    )
    result = _runtime(ledger).run(
        _calc_spec(), ScriptedStrategy([SubmitPlan(bad), Finish("done")])
    )

    assert result.accepted is False
    assert any(
        event.kind is EventKind.BINDING_FAILED
        and event.payload.get("status") == "invalid_plan"
        for event in ledger.read()
    )


def test_finalizer_does_not_commit_for_a_false_claim() -> None:
    ledger = Ledger.in_memory("task-1")
    finalizer = FakeFinalizer()
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
        finalizer=finalizer,
    )
    false_claim = Claim(
        id="false",
        kind=PropositionKind.FILE_CHANGED,
        target="missing.py",
        source=ClaimSource.LLM,
    )

    result = runtime.run(
        _calc_spec(),
        ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.EDIT)),
                Finish("fixed", claims=(false_claim,)),
            ]
        ),
    )

    assert result.accepted is False
    assert finalizer.calls == 0
    assert result.finalize_sha is None


def test_finalizer_satisfies_a_commit_step_without_a_model_commit() -> None:
    ledger = Ledger.in_memory("task-1")
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
        finalizer=FakeFinalizer(),
    )
    result = runtime.run(
        _calc_spec(),
        ScriptedStrategy(
            [
                SubmitPlan(_plan()),
                CallTool(ToolIntent(capability=Capability.EDIT)),
                CallTool(ToolIntent(capability=Capability.VERIFY)),
                Finish(
                    "done",
                    claims=(
                        Claim(
                            id="commit-finalizer",
                            kind=PropositionKind.COMMIT_CREATED,
                            source=ClaimSource.LLM,
                        ),
                    ),
                ),
            ]
        ),
    )

    assert result.accepted is True
    assert result.finalize_sha == "def5678"
    assert len(ledger.read(EventKind.STEP_CLOSED)) == 3

    events = ledger.read()
    preflight = next(
        index
        for index, event in enumerate(events)
        if event.kind is EventKind.AUDIT_REPORTED
        and event.payload.get("scope") == "preflight"
    )
    intent = next(
        index
        for index, event in enumerate(events)
        if event.kind is EventKind.DELIVERY_ATTEMPTED
        and event.payload.get("status") == "intent"
    )
    receipt = next(
        index
        for index, event in enumerate(events)
        if event.kind is EventKind.TOOL_RECEIPT
        and event.payload.get("receipt", {}).get("tool") == "finalizer"
    )
    assert preflight < intent < receipt


class BadFinalizer(FakeFinalizer):
    def __init__(self) -> None:
        super().__init__()
        self.rolled_back: list[str] = []

    def finalize(self, **kwargs):
        self.calls += 1
        del kwargs
        return type(
            "Outcome",
            (),
            {
                "accepted": True,
                "created": True,
                "commit_sha": "def5678",
                "files": ("outside.py",),
                "reason": "",
            },
        )()

    def rollback(self, commit_sha: str) -> tuple[bool, str]:
        self.rolled_back.append(commit_sha)
        return True, ""


def test_plan_only_mode_stops_after_accepted_plan() -> None:
    ledger = Ledger.in_memory("task-1")
    gateway = CountingGateway()
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=gateway,
        controller=VerificationController(),
        ledger=ledger,
    )

    result = runtime.run(
        replace(_calc_spec(), mode=TaskMode.PLAN),
        ScriptedStrategy(
            [
                SubmitPlan(_plan()),
                CallTool(ToolIntent(capability=Capability.EDIT)),
            ]
        ),
    )

    assert result.phase is TaskPhase.PLANNED
    assert result.accepted is False
    assert "edit, verify, record" in result.answer
    assert "1. edit · calc/add.py" in result.answer
    assert result.debt == ()
    assert gateway.calls == 0
    assert ledger.read(EventKind.TOOL_RECEIPT) == ()
    assert ledger.read(EventKind.STEP_STARTED) == ()
    assert len(ledger.read(EventKind.PLAN_ACCEPTED)) == 1


def test_plan_only_renders_a_plan_without_analysis() -> None:
    ledger = Ledger.in_memory("task-1")
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=CountingGateway(),
        controller=VerificationController(),
        ledger=ledger,
    )
    plan = PlanProposal(
        steps=(StepContract(id=7, kind=StepKind.THINK),)
    )

    result = runtime.run(
        replace(_calc_spec(), mode=TaskMode.PLAN),
        ScriptedStrategy([SubmitPlan(plan)]),
    )

    assert result.phase is TaskPhase.PLANNED
    assert result.answer == "7. think"


def _ledger_plan_digest(ledger: Ledger) -> str:
    accepted = ledger.read(EventKind.PLAN_ACCEPTED)[-1]
    return hashlib.sha256(
        canonical_json(accepted.payload["plan"]).encode("utf-8")
    ).hexdigest()


def test_resume_planned_executes_approved_plan_in_same_ledger() -> None:
    ledger = Ledger.in_memory("task-1")
    plan = PlanProposal(
        analysis="edit then verify",
        steps=_plan().steps[:2],
    )
    planned = _runtime(ledger).run(
        replace(_calc_spec(), mode=TaskMode.PLAN),
        ScriptedStrategy([SubmitPlan(plan)]),
    )
    assert planned.phase is TaskPhase.PLANNED

    result = _runtime(ledger).resume_planned(
        _calc_spec(),
        ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.EDIT)),
                CallTool(ToolIntent(capability=Capability.VERIFY)),
                Finish("done"),
            ]
        ),
        plan,
        plan_digest=_ledger_plan_digest(ledger),
        approval_id="approval-1",
    )

    events = ledger.read()
    assert result.phase is TaskPhase.FINALIZED
    assert result.accepted is True
    assert len(ledger.read(EventKind.TASK_STARTED)) == 1
    assert len(ledger.read(EventKind.PLAN_ACCEPTED)) == 1
    approved = ledger.read(EventKind.PLAN_APPROVED)
    assert len(approved) == 1
    assert approved[0].payload["approval_id"] == "approval-1"
    assert approved[0].payload["approved"] is True
    assert len(ledger.read(EventKind.STEP_STARTED)) == 2
    assert events[-1].kind is not EventKind.TASK_STARTED


def test_resume_planned_rejects_wrong_digest_and_missing_approval() -> None:
    ledger = Ledger.in_memory("task-1")
    plan = PlanProposal(analysis="edit then verify", steps=_plan().steps[:2])
    _runtime(ledger).run(
        replace(_calc_spec(), mode=TaskMode.PLAN),
        ScriptedStrategy([SubmitPlan(plan)]),
    )

    with pytest.raises(ValueError, match="approval_id"):
        _runtime(ledger).resume_planned(
            _calc_spec(),
            ScriptedStrategy([]),
            plan,
            plan_digest=_ledger_plan_digest(ledger),
            approval_id="",
        )
    with pytest.raises(ValueError, match="plan_digest"):
        _runtime(ledger).resume_planned(
            _calc_spec(),
            ScriptedStrategy([]),
            plan,
            plan_digest="",
            approval_id="approval-1",
        )
    with pytest.raises(ValueError, match="digest"):
        _runtime(ledger).resume_planned(
            _calc_spec(),
            ScriptedStrategy([]),
            plan,
            plan_digest="not-the-plan",
            approval_id="approval-1",
        )


def test_resume_planned_rejects_plan_mode_and_missing_accepted_plan() -> None:
    fresh = Ledger.in_memory("task-1")
    plan = PlanProposal(analysis="edit", steps=_plan().steps[:1])
    with pytest.raises(ValueError, match="execution TaskMode"):
        _runtime(fresh).resume_planned(
            replace(_calc_spec(), mode=TaskMode.PLAN),
            ScriptedStrategy([]),
            plan,
            plan_digest="x",
            approval_id="approval-1",
        )
    with pytest.raises(ValueError, match="no accepted plan"):
        _runtime(fresh).resume_planned(
            _calc_spec(),
            ScriptedStrategy([]),
            plan,
            plan_digest="x",
            approval_id="approval-1",
        )


def test_plan_only_invalid_plan_is_blocked_not_planned() -> None:
    ledger = Ledger.in_memory("task-1")
    invalid = PlanProposal(
        analysis="duplicate step id",
        steps=(_plan().steps[0], _plan().steps[0]),
    )
    result = _runtime(ledger).run(
        replace(_calc_spec(), mode=TaskMode.PLAN),
        ScriptedStrategy([SubmitPlan(invalid)]),
    )

    assert result.phase is TaskPhase.BLOCKED
    assert "计划生成失败" in result.answer
    assert ledger.read(EventKind.PLAN_ACCEPTED) == ()


def test_plan_only_empty_plan_is_blocked_not_planned() -> None:
    ledger = Ledger.in_memory("task-1")
    empty = PlanProposal(analysis="empty", steps=())
    result = _runtime(ledger).run(
        replace(_calc_spec(), mode=TaskMode.PLAN),
        ScriptedStrategy([SubmitPlan(empty)]),
    )

    assert result.phase is TaskPhase.BLOCKED
    assert result.accepted is False
    assert result.answer
    assert ledger.read(EventKind.PLAN_ACCEPTED) == ()
    assert ledger.read(EventKind.STEP_STARTED) == ()


def test_resume_planned_rejects_existing_execution_events() -> None:
    second = Ledger.in_memory("task-2")
    plan = PlanProposal(analysis="edit", steps=_plan().steps[:1])
    _runtime(second).run(
        replace(_calc_spec(), task_id="task-2", mode=TaskMode.PLAN),
        ScriptedStrategy([SubmitPlan(plan)]),
    )
    second.append(
        EventKind.STEP_STARTED,
        EventSource.RUNTIME,
        {"step_id": 1, "task": "task-1", "kind": "edit"},
        step_id="1",
    )
    with pytest.raises(ValueError, match="execution events"):
        _runtime(second).resume_planned(
            replace(_calc_spec(), task_id="task-2"),
            ScriptedStrategy([]),
            plan,
            plan_digest=_ledger_plan_digest(second),
            approval_id="approval-1",
        )


def test_resume_planned_rejects_a_second_resume() -> None:
    ledger = Ledger.in_memory("task-1")
    plan = PlanProposal(analysis="edit then verify", steps=_plan().steps[:2])
    _runtime(ledger).run(
        replace(_calc_spec(), mode=TaskMode.PLAN),
        ScriptedStrategy([SubmitPlan(plan)]),
    )
    runtime = _runtime(ledger)
    runtime.resume_planned(
        _calc_spec(),
        ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.EDIT)),
                CallTool(ToolIntent(capability=Capability.VERIFY)),
                Finish("done"),
            ]
        ),
        plan,
        plan_digest=_ledger_plan_digest(ledger),
        approval_id="approval-1",
    )

    with pytest.raises(ValueError, match="planned phase"):
        runtime.resume_planned(
            _calc_spec(),
            ScriptedStrategy([]),
            plan,
            plan_digest=_ledger_plan_digest(ledger),
            approval_id="approval-2",
        )


def test_plan_only_mode_refuses_non_plan_actions() -> None:
    ledger = Ledger.in_memory("task-1")
    gateway = CountingGateway()
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=gateway,
        controller=VerificationController(),
        ledger=ledger,
    )

    result = runtime.run(
        replace(_calc_spec(), mode=TaskMode.PLAN),
        ScriptedStrategy(
            [CallTool(ToolIntent(capability=Capability.EDIT))]
        ),
    )

    assert result.phase is TaskPhase.BLOCKED
    assert result.accepted is False
    assert gateway.calls == 0
    assert ledger.read(EventKind.TOOL_RECEIPT) == ()


def test_blocked_delivery_rolls_back_the_finalizer_commit() -> None:
    ledger = Ledger.in_memory("task-1")
    finalizer = BadFinalizer()
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
        finalizer=finalizer,
    )

    result = runtime.run(
        _calc_spec(),
        ScriptedStrategy(
            [
                SubmitPlan(_plan()),
                CallTool(ToolIntent(capability=Capability.EDIT)),
                CallTool(ToolIntent(capability=Capability.VERIFY)),
                Finish("done"),
            ]
        ),
    )

    assert result.accepted is False
    assert finalizer.rolled_back == ["def5678"]
    from xenon.ledger.projections import project_task_state

    state = project_task_state(ledger.read())
    assert all(item.get("sha") != "def5678" for item in state["commits"])
    assert any(
        event.kind is EventKind.DELIVERY_ATTEMPTED
        and event.payload.get("status") == "rolled_back"
        for event in ledger.read()
    )
    assert any(
        event.kind is EventKind.AUDIT_REPORTED
        and event.payload.get("scope") == "preflight"
        for event in ledger.read()
    )


def test_dag_ready_step_is_selected_before_plan_order() -> None:
    ledger = Ledger.in_memory("task-1")
    step1 = StepContract(
        id=1,
        kind=StepKind.EDIT,
        intent=ToolIntent(capability=Capability.EDIT),
        expects=(
            Obligation(
                id="dag-edit",
                kind=PropositionKind.FILE_CHANGED,
                target="calc/add.py",
                source=ObligationSource.PLAN,
            ),
        ),
    )
    step2 = StepContract(
        id=2,
        kind=StepKind.VERIFY,
        intent=ToolIntent(capability=Capability.VERIFY),
        depends_on=(1,),
        expects=(
            Obligation(
                id="dag-test",
                kind=PropositionKind.TEST_PASSED,
                target="tests/test_calc.py",
                source=ObligationSource.PLAN,
            ),
        ),
    )
    plan = PlanProposal(steps=(step2, step1))

    result = _runtime(ledger).run(_calc_spec(), _follow(plan))

    assert result.accepted is True
    started = [
        event.payload["step_id"]
        for event in ledger.read(EventKind.STEP_STARTED)
    ]
    assert started == [1, 2]


def test_cyclic_plan_is_rejected() -> None:
    ledger = Ledger.in_memory("task-1")
    first = StepContract(
        id=1,
        kind=StepKind.EDIT,
        intent=ToolIntent(capability=Capability.EDIT),
        depends_on=(2,),
    )
    second = StepContract(
        id=2,
        kind=StepKind.VERIFY,
        intent=ToolIntent(capability=Capability.VERIFY),
        depends_on=(1,),
    )
    result = _runtime(ledger).run(
        _calc_spec(),
        ScriptedStrategy(
            [SubmitPlan(PlanProposal(steps=(first, second))), Finish("done")]
        ),
    )

    assert result.accepted is False
    assert any(
        event.kind is EventKind.BINDING_FAILED
        and event.payload.get("status") == "invalid_plan"
        and "cycle" in str(event.payload.get("reason"))
        for event in ledger.read()
    )


def test_plan_first_closes_each_step_and_advances() -> None:
    ledger = Ledger.in_memory("task-1")
    runtime = _runtime(ledger)
    result = runtime.run(_calc_spec(), _follow(_plan()))

    assert result.accepted is True
    assert result.evidence_status.value == "verified"
    closed = ledger.read(EventKind.STEP_CLOSED)
    assert [event.payload["step_id"] for event in closed] == [1, 2, 3]
    started = ledger.read(EventKind.STEP_STARTED)
    assert [event.payload["step_id"] for event in started] == [1, 2, 3]
    plan = ledger.project("plan")
    assert plan["steps"]["1"]["status"] == "closed"
    assert plan["steps"]["3"]["status"] == "closed"


def test_finishing_before_a_plan_step_blocks_delivery() -> None:
    ledger = Ledger.in_memory("task-1")
    runtime = _runtime(ledger)
    script = ScriptedStrategy(
        [
            SubmitPlan(_plan()),
            CallTool(ToolIntent(capability=Capability.EDIT)),
            Finish("done too early"),
        ]
    )

    result = runtime.run(_calc_spec(), script)

    assert result.accepted is False
    assert result.phase.value == "blocked"
    assert any(item.obligation.id == "test-1" for item in result.debt)
    assert any(
        event.kind is EventKind.DEBT_OPENED
        and event.payload["obligation_id"] == "test-1"
        for event in ledger.read()
    )


def test_finish_without_any_evidence_does_not_deliver() -> None:
    ledger = Ledger.in_memory("task-1")
    result = _runtime(ledger).run(
        _calc_spec(), ScriptedStrategy([Finish("all done")])
    )

    assert result.accepted is False
    assert result.phase.value == "blocked"
    assert result.debt
    assert result.evidence_status.value == "unverified"


def test_prior_commit_is_not_reused_for_a_later_task() -> None:
    ledger = Ledger.in_memory("task-1")
    first = _runtime(ledger).run(
        _calc_spec(),
        ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.READ)),
                CallTool(ToolIntent(capability=Capability.EDIT)),
                CallTool(ToolIntent(capability=Capability.VCS)),
                Finish("recorded"),
            ]
        ),
    )
    assert first.finalize_sha == "abc1234"

    second = _runtime(ledger).run(
        _calc_spec(),
        ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.READ)),
                Finish("read only"),
            ]
        ),
    )
    assert second.accepted is True
    assert second.finalize_sha is None


def test_claim_resolution_is_reported_not_hidden() -> None:
    ledger = Ledger.in_memory("task-1")
    lie = Claim(
        id="claim-lie",
        kind=PropositionKind.FILE_CHANGED,
        target="calc/add.py",
        source=ClaimSource.LLM,
    )
    result = _runtime(ledger).run(
        _calc_spec(), ScriptedStrategy([Finish("fixed", claims=(lie,))])
    )

    assert result.accepted is False
    assert result.resolutions
    assert result.resolutions[0].status is ResolutionStatus.UNRESOLVED


class BadRollbackFinalizer(BadFinalizer):
    def rollback(self, commit_sha: str) -> tuple[bool, str]:
        self.rolled_back.append(commit_sha)
        return False, "worktree is locked"


def test_failed_delivery_rollback_blocks_with_debt() -> None:
    ledger = Ledger.in_memory("task-1")
    finalizer = BadRollbackFinalizer()
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
        finalizer=finalizer,
    )

    result = runtime.run(
        _calc_spec(),
        ScriptedStrategy(
            [
                SubmitPlan(_plan()),
                CallTool(ToolIntent(capability=Capability.EDIT)),
                CallTool(ToolIntent(capability=Capability.VERIFY)),
                Finish("done"),
            ]
        ),
    )

    assert result.accepted is False
    assert result.phase.value == "blocked"
    assert any(item.obligation.id == "delivery-rollback" for item in result.debt)
    assert any(
        event.kind is EventKind.DELIVERY_ATTEMPTED
        and event.payload.get("status") == "rollback_failed"
        for event in ledger.read()
    )


class RaisingFinalizer(FakeFinalizer):
    def finalize(self, **kwargs):
        self.calls += 1
        del kwargs
        raise RuntimeError("finalizer exploded")


class RejectedFinalizer(FakeFinalizer):
    def finalize(self, **kwargs):
        self.calls += 1
        del kwargs
        return type(
            "Outcome",
            (),
            {
                "accepted": False,
                "created": False,
                "commit_sha": None,
                "files": (),
                "reason": "rejected by test",
            },
        )()


class NoRollbackFinalizer(BadFinalizer):
    rollback = None


class RaisingRollbackFinalizer(BadFinalizer):
    def rollback(self, commit_sha: str) -> tuple[bool, str]:
        raise RuntimeError("rollback exploded")


def _run_finish(finalizer):
    ledger = Ledger.in_memory("task-1")
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
        finalizer=finalizer,
    )
    result = runtime.run(
        _calc_spec(),
        ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.EDIT)),
                Finish("done"),
            ]
        ),
    )
    return ledger, result


def test_finalizer_exception_opens_debt() -> None:
    ledger, result = _run_finish(RaisingFinalizer())

    assert result.accepted is False
    assert any(item.obligation.id == "finalizer" for item in result.debt)
    assert any(
        event.kind is EventKind.DELIVERY_ATTEMPTED
        and event.payload.get("status") == "intent"
        for event in ledger.read()
    )


def test_finalizer_rejection_opens_debt() -> None:
    ledger, result = _run_finish(RejectedFinalizer())

    assert result.accepted is False
    assert any(item.obligation.id == "finalizer" for item in result.debt)
    assert any(
        event.kind is EventKind.DELIVERY_ATTEMPTED
        and event.payload.get("status") == "intent"
        for event in ledger.read()
    )


def test_missing_rollback_capability_fails_closed() -> None:
    _ledger, result = _run_finish(NoRollbackFinalizer())

    assert result.accepted is False
    assert any(
        item.obligation.id == "delivery-rollback" for item in result.debt
    )


def test_rollback_exception_fails_closed() -> None:
    _ledger, result = _run_finish(RaisingRollbackFinalizer())

    assert result.accepted is False
    assert any(
        item.obligation.id == "delivery-rollback" for item in result.debt
    )
