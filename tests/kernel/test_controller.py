"""Tests for the deterministic verification controller (#199)."""

from __future__ import annotations

from xenon.kernel.contracts import (
    Claim,
    ClaimSource,
    ControlDecisionKind,
    Debt,
    EffectKind,
    FsWrite,
    Obligation,
    ObligationSource,
    PropositionKind,
    ReceiptStatus,
    StepContract,
    StepKind,
    TestRun,
    ToolReceipt,
    VcsCommit,
    Verdict,
    VerdictStatus,
    VerificationStage,
)
from xenon.kernel.controller import ClaimResolver, VerificationController
from xenon.ledger.store import Ledger


def _ledger() -> Ledger:
    return Ledger.in_memory("t1")


def _append(
    ledger: Ledger,
    effects: tuple,
    *,
    status: ReceiptStatus = ReceiptStatus.OK,
    declared: tuple = (),
    observed: tuple = (),
) -> ToolReceipt:
    receipt = ToolReceipt(
        receipt_id=f"r-{ledger.watermark() + 1}",
        call_id=f"c-{ledger.watermark() + 1}",
        task_id="t1",
        tool="mutate",
        status=status,
        effects=effects,
        declared_effects=declared or tuple(item.kind for item in effects),
        observed_effects=observed or tuple(item.kind for item in effects),
    )
    ledger.append_receipt(receipt)
    return receipt


def _claim(kind: PropositionKind, target: str = "", **expect) -> Claim:
    return Claim(
        id=f"claim-{kind.value}",
        kind=kind,
        target=target,
        expect=expect,
        source=ClaimSource.LLM,
    )


def test_file_changed_binds_to_a_real_write() -> None:
    ledger = _ledger()
    _append(ledger, (FsWrite(paths=("calc/add.py",)),))
    resolution = ClaimResolver().resolve(
        _claim(PropositionKind.FILE_CHANGED, "calc/add.py"), ledger
    )
    assert resolution.status.value == "bound"
    assert len(resolution.fact_ids) == 1


def test_noop_write_does_not_satisfy_file_changed() -> None:
    ledger = _ledger()
    _append(ledger, (FsWrite(paths=("calc/add.py",), changed=False),))
    resolution = ClaimResolver().resolve(
        _claim(PropositionKind.FILE_CHANGED, "calc/add.py"), ledger
    )
    assert resolution.status.value == "unresolved"


def test_baseline_excludes_older_facts() -> None:
    ledger = _ledger()
    _append(ledger, (FsWrite(paths=("a.py",)),))
    baseline = ledger.watermark()
    claim = Claim(
        id="c1",
        kind=PropositionKind.FILE_CHANGED,
        target="a.py",
        source=ClaimSource.LLM,
        since_seq=baseline,
    )
    resolver = ClaimResolver()
    assert resolver.resolve(claim, ledger).status.value == "unresolved"

    _append(ledger, (FsWrite(paths=("b.py",)),))
    newer = Claim(
        id="c2",
        kind=PropositionKind.FILE_CHANGED,
        target="b.py",
        source=ClaimSource.LLM,
        since_seq=baseline,
    )
    assert resolver.resolve(newer, ledger).status.value == "bound"


def test_test_passed_requires_real_tests_and_target() -> None:
    ledger = _ledger()
    _append(
        ledger,
        (
            TestRun(
                target="tests/test_a.py",
                tests_run=1,
                passed=1,
                exit_code=0,
            ),
        ),
    )
    resolver = ClaimResolver()
    assert (
        resolver.resolve(
            _claim(PropositionKind.TEST_PASSED, "tests/test_a.py"), ledger
        ).status.value
        == "bound"
    )
    assert (
        resolver.resolve(
            _claim(PropositionKind.TEST_PASSED, "tests/test_b.py"), ledger
        ).status.value
        == "unresolved"
    )


def test_pytest_node_id_matches_the_test_file_target() -> None:
    ledger = _ledger()
    _append(
        ledger,
        (
            TestRun(
                target="tests/test_a.py::test_add",
                tests_run=1,
                passed=1,
                exit_code=0,
            ),
        ),
    )
    resolution = ClaimResolver().resolve(
        _claim(PropositionKind.TEST_PASSED, "tests/test_a.py"), ledger
    )
    assert resolution.status.value == "bound"


def test_zero_tests_is_not_a_passing_test() -> None:
    ledger = _ledger()
    _append(
        ledger,
        (TestRun(target="tests/test_a.py", tests_run=0, passed=0, exit_code=0),),
    )
    resolution = ClaimResolver().resolve(
        _claim(PropositionKind.TEST_PASSED, "tests/test_a.py"), ledger
    )
    assert resolution.status.value == "unresolved"


def test_commit_claim_matches_real_sha() -> None:
    ledger = _ledger()
    _append(ledger, (VcsCommit(sha="abc1234"),))
    resolution = ClaimResolver().resolve(
        _claim(PropositionKind.COMMIT_CREATED, "abc"), ledger
    )
    assert resolution.status.value == "bound"


def test_ambiguous_without_target_is_explicit() -> None:
    ledger = _ledger()
    _append(ledger, (FsWrite(paths=("a.py",)),))
    _append(ledger, (FsWrite(paths=("b.py",)),))
    resolution = ClaimResolver().resolve(
        _claim(PropositionKind.FILE_CHANGED), ledger
    )
    assert resolution.status.value == "ambiguous"


def test_reconcile_flags_failed_receipts_and_effect_drift() -> None:
    controller = VerificationController()
    failed = ToolReceipt(
        receipt_id="r1",
        call_id="c1",
        task_id="t1",
        tool="mutate",
        status=ReceiptStatus.FAILED,
    )
    assert (
        controller.reconcile(None, failed).status is VerdictStatus.VIOLATED
    )

    drift = ToolReceipt(
        receipt_id="r2",
        call_id="c2",
        task_id="t1",
        tool="mutate",
        status=ReceiptStatus.OK,
        declared_effects=(EffectKind.FS_WRITE,),
        observed_effects=(),
    )
    assert (
        controller.reconcile(None, drift).status
        is VerdictStatus.UNVERIFIABLE
    )


def test_reconcile_rejects_failed_and_zero_test_runs() -> None:
    controller = VerificationController()
    failed = ToolReceipt(
        receipt_id="r1",
        call_id="c1",
        task_id="t1",
        tool="check",
        status=ReceiptStatus.OK,
        effects=(TestRun(target="tests/test_a.py", exit_code=1, tests_run=1),),
        declared_effects=(EffectKind.TEST_RUN,),
        observed_effects=(EffectKind.TEST_RUN,),
    )
    assert (
        controller.reconcile(None, failed).status is VerdictStatus.VIOLATED
    )
    zero = ToolReceipt(
        receipt_id="r2",
        call_id="c2",
        task_id="t1",
        tool="check",
        status=ReceiptStatus.OK,
        effects=(TestRun(target="tests/test_a.py", tests_run=0),),
        declared_effects=(EffectKind.TEST_RUN,),
        observed_effects=(EffectKind.TEST_RUN,),
    )
    assert (
        controller.reconcile(None, zero).status
        is VerdictStatus.UNVERIFIABLE
    )


def test_close_step_evaluates_step_expectations() -> None:
    ledger = _ledger()
    _append(ledger, (FsWrite(paths=("a.py",)),))
    step = StepContract(
        id=1,
        kind=StepKind.EDIT,
        expects=(
            Obligation(
                id="o1",
                kind=PropositionKind.FILE_CHANGED,
                target="a.py",
                source=ObligationSource.PLAN,
            ),
        ),
    )
    verdicts = VerificationController().close_step(step, ledger)
    assert verdicts[0].stage is VerificationStage.V1
    assert verdicts[0].status is VerdictStatus.SATISFIED

    missing = StepContract(
        id=2,
        kind=StepKind.EDIT,
        expects=(
            Obligation(
                id="o2",
                kind=PropositionKind.FILE_CHANGED,
                target="b.py",
                source=ObligationSource.PLAN,
            ),
        ),
    )
    assert (
        VerificationController().close_step(missing, ledger)[0].status
        is VerdictStatus.VIOLATED
    )


def test_audit_binds_or_rejects_a_claim() -> None:
    ledger = _ledger()
    _append(ledger, (FsWrite(paths=("a.py",)),))
    controller = VerificationController()
    bound = controller.audit(
        _claim(PropositionKind.FILE_CHANGED, "a.py"), ledger, ()
    )
    assert bound.status is VerdictStatus.SATISFIED
    assert bound.fact_ids

    missing = controller.audit(
        _claim(PropositionKind.FILE_CHANGED, "b.py"), ledger, ()
    )
    assert missing.status is VerdictStatus.VIOLATED


def test_audit_reports_open_debt_as_unverifiable() -> None:
    debt = Debt(
        obligation=Obligation(id="o1", kind=PropositionKind.REQUIREMENT_MET),
        reason="missing evidence",
    )
    verdict = VerificationController().audit(None, _ledger(), (debt,))
    assert verdict.status is VerdictStatus.UNVERIFIABLE


def test_decide_is_single_authority() -> None:
    controller = VerificationController()
    satisfied = Verdict(
        obligation_id="o1",
        stage=VerificationStage.V2,
        status=VerdictStatus.SATISFIED,
    )
    assert (
        controller.decide((satisfied,), ()).kind is ControlDecisionKind.DELIVER
    )
    violated = Verdict(
        obligation_id="o2",
        stage=VerificationStage.V2,
        status=VerdictStatus.VIOLATED,
        reason="no fact",
    )
    assert (
        controller.decide((violated,), ()).kind is ControlDecisionKind.BLOCK
    )
    unverifiable = Verdict(
        obligation_id="o3",
        stage=VerificationStage.V2,
        status=VerdictStatus.UNVERIFIABLE,
        reason="abstain",
    )
    assert (
        controller.decide((unverifiable,), ()).kind
        is ControlDecisionKind.ABSTAIN
    )
