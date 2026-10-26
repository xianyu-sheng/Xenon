"""Plan step lifecycle and Finish fail-closed tests (#198)."""

from __future__ import annotations

from tests.kernel.test_north_star import (
    FakeCatalog,
    FakeGateway,
    ScriptedStrategy,
    _calc_spec,
)
from xenon.kernel.contracts import (
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
    TestRun,
    ToolIntent,
    ToolReceipt,
)
from xenon.kernel.controller import VerificationController
from xenon.kernel.runtime import LoopRuntime
from xenon.kernel.strategies import PlanFirst
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


def _runtime(ledger: Ledger) -> LoopRuntime:
    return LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
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

    result = runtime.run(_calc_spec(), PlanFirst(lambda view: plan))

    assert result.accepted is True
    assert not result.debt
    assert any(event.kind is EventKind.DEBT_CLOSED for event in ledger.read())


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


def test_plan_first_closes_each_step_and_advances() -> None:
    ledger = Ledger.in_memory("task-1")
    runtime = _runtime(ledger)
    result = runtime.run(_calc_spec(), PlanFirst(lambda view: _plan()))

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
