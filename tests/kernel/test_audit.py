"""Deterministic V2 coverage and negative facts (#200)."""

from __future__ import annotations

from tests.kernel.test_north_star import (
    FakeCatalog,
    FakeGateway,
    ScriptedStrategy,
)
from xenon.kernel.audit import AuditCoordinator, CoverageBinder
from xenon.kernel.contracts import (
    AuditProposal,
    CallTool,
    Capability,
    CoverageClaim,
    Finish,
    FsWrite,
    Obligation,
    PropositionKind,
    ReceiptStatus,
    ResolutionStatus,
    TaskSpec,
    TestRun,
    ToolIntent,
    ToolReceipt,
    VcsPush,
    VerdictStatus,
)
from xenon.kernel.controller import VerificationController
from xenon.kernel.runtime import LoopRuntime
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


def _append(ledger: Ledger, *effects) -> None:
    receipt = ToolReceipt(
        receipt_id=f"r-{ledger.watermark() + 1}",
        call_id=f"c-{ledger.watermark() + 1}",
        task_id="task-1",
        tool="tool",
        status=ReceiptStatus.OK,
        effects=tuple(effects),
        declared_effects=tuple(item.kind for item in effects),
        observed_effects=tuple(item.kind for item in effects),
    )
    ledger.append_receipt(receipt)


def _obligation(kind: PropositionKind, target: str = "", **kwargs):
    return Obligation(
        id=f"o-{kind.value}",
        kind=kind,
        target=target,
        source=kwargs.pop("source", None) or __import__(
            "xenon.kernel.contracts", fromlist=["ObligationSource"]
        ).ObligationSource.USER,
        expect=kwargs.pop("expect", {}),
    )


def test_binder_binds_file_and_test_facts() -> None:
    ledger = Ledger.in_memory("task-1")
    _append(
        ledger,
        FsWrite(paths=("calc/add.py",), hashes={"calc/add.py": "h"}),
        TestRun(
            target="tests/test_add.py",
            tests_run=1,
            passed=1,
            exit_code=0,
        ),
    )
    file_row = CoverageBinder().bind(
        _obligation(PropositionKind.FILE_CHANGED, "calc/add.py"), ledger
    )
    test_row = CoverageBinder().bind(
        _obligation(PropositionKind.TEST_PASSED, "tests/test_add.py"), ledger
    )
    assert file_row.status is ResolutionStatus.BOUND
    assert test_row.status is ResolutionStatus.BOUND
    assert test_row.fact_ids


def test_binder_marks_missing_evidence_unresolved() -> None:
    ledger = Ledger.in_memory("task-1")
    row = CoverageBinder().bind(
        _obligation(PropositionKind.FILE_CREATED, "calc/mul.py"), ledger
    )
    assert row.status is ResolutionStatus.UNRESOLVED
    assert "create" in row.next_action


def test_no_push_is_a_negative_fact() -> None:
    obligation = _obligation(
        PropositionKind.PUSHED, "origin", expect={"absent": True}
    )
    empty = Ledger.in_memory("task-1")
    bound = CoverageBinder().bind(obligation, empty)
    assert bound.status is ResolutionStatus.BOUND
    assert "negative fact" in bound.reason

    pushed = Ledger.in_memory("task-1")
    _append(pushed, VcsPush(remote="origin", branch="main", sha="abc"))
    violated = CoverageBinder().bind(obligation, pushed)
    assert violated.status is ResolutionStatus.UNRESOLVED
    assert violated.fact_ids


def test_semantic_requirement_abstains_without_deterministic_check() -> None:
    ledger = Ledger.in_memory("task-1")
    row = CoverageBinder().bind(
        _obligation(PropositionKind.REQUIREMENT_MET, "support expressions"),
        ledger,
    )
    assert row.status is ResolutionStatus.ABSTAINED
    assert "deterministic evidence" in row.next_action


def test_proposal_can_bind_only_to_real_facts() -> None:
    ledger = Ledger.in_memory("task-1")
    _append(ledger, FsWrite(paths=("calc/add.py",), hashes={"calc/add.py": "h"}))
    obligation = _obligation(PropositionKind.REQUIREMENT_MET, "add")
    proposal = AuditProposal(
        coverage=(
            CoverageClaim(
                obligation_id=obligation.id,
                artifact_paths=("calc/add.py",),
            ),
        )
    )
    report = AuditCoordinator().audit((obligation,), ledger, proposal)
    assert report.coverage[0].status is ResolutionStatus.BOUND
    assert report.coverage[0].source == "proposal"
    assert report.verdicts[0].status is VerdictStatus.SATISFIED


def test_unresolved_obligation_becomes_v2_debt_without_finalizer() -> None:
    ledger = Ledger.in_memory("task-1")
    spec = TaskSpec(
        task_id="task-1",
        objective="add multiply",
        workspace=".",
        obligations=(
            Obligation(
                id="mul-created",
                kind=PropositionKind.FILE_CREATED,
                target="calc/mul.py",
            ),
        ),
    )
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
    )
    result = runtime.run(
        spec,
        ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.EDIT)),
                Finish("done"),
            ]
        ),
    )

    assert result.accepted is False
    assert any(item.obligation.id == "mul-created" for item in result.debt)
    audit = [
        event
        for event in ledger.read(EventKind.AUDIT_REPORTED)
        if event.payload.get("scope") == "final"
    ][-1]
    assert audit.payload["coverage"][0]["obligation_id"] == "mul-created"
    assert audit.payload["coverage"][0]["status"] == "unresolved"


def test_preflight_audit_prevents_finalizer_commit() -> None:
    class Finalizer:
        calls = 0

        def finalize(self, **kwargs):
            Finalizer.calls += 1
            return type(
                "Outcome",
                (),
                {
                    "accepted": True,
                    "created": True,
                    "commit_sha": "abc1234",
                    "files": ("calc/add.py",),
                    "reason": "",
                },
            )()

    ledger = Ledger.in_memory("task-1")
    spec = TaskSpec(
        task_id="task-1",
        objective="add multiply",
        workspace=".",
        obligations=(
            Obligation(
                id="mul-created",
                kind=PropositionKind.FILE_CREATED,
                target="calc/mul.py",
            ),
        ),
    )
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
        finalizer=Finalizer(),
    )
    result = runtime.run(
        spec,
        ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.EDIT)),
                Finish("done"),
            ]
        ),
    )

    assert result.accepted is False
    assert Finalizer.calls == 0
    assert any(item.obligation.id == "mul-created" for item in result.debt)


class FakeAuditProvider:
    def __init__(self) -> None:
        self.calls = 0

    def propose(self, obligations, ledger):
        self.calls += 1
        del ledger
        return AuditProposal(
            coverage=(
                CoverageClaim(
                    obligation_id="add-requirement",
                    artifact_paths=("calc/add.py",),
                    rationale="the write fact covers the requirement",
                ),
            )
        )


def test_audit_provider_is_only_a_proposal() -> None:
    ledger = Ledger.in_memory("task-1")
    provider = FakeAuditProvider()
    spec = TaskSpec(
        task_id="task-1",
        objective="add",
        workspace=".",
        obligations=(
            Obligation(
                id="add-requirement",
                kind=PropositionKind.REQUIREMENT_MET,
                target="add",
            ),
        ),
    )
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
        audit_provider=provider,
    )
    result = runtime.run(
        spec,
        ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.EDIT)),
                Finish("done"),
            ]
        ),
    )

    assert result.accepted is True
    assert provider.calls == 1
    assert result.metrics["audit_rounds"] == 1.0
    proposed = next(
        event for event in ledger.read(EventKind.AUDIT_PROPOSED)
    )
    assert proposed.payload["status"] == "proposed"
    assert proposed.payload["coverage"][0]["obligation_id"] == "add-requirement"


def test_audit_round_budget_blocks_before_a_second_audit() -> None:
    from xenon.kernel.contracts import Budget

    ledger = Ledger.in_memory("task-1")
    spec = TaskSpec(
        task_id="task-1",
        objective="do work",
        workspace=".",
        budget=Budget(max_audit_rounds=0),
    )
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
    )
    result = runtime.run(
        spec,
        ScriptedStrategy([Finish("done")]),
    )

    assert result.accepted is False
    assert result.phase.value == "blocked"
    assert any(
        event.kind is EventKind.POLICY_DECISION
        and "audit budget exhausted" in str(event.payload.get("reason"))
        for event in ledger.read()
    )


class RaisingAuditProvider:
    def propose(self, obligations, ledger):
        raise RuntimeError("provider failed")


class NoneAuditProvider:
    def propose(self, obligations, ledger):
        return None


class InvalidAuditProvider:
    def propose(self, obligations, ledger):
        return object()


def _audit_provider_result(provider):
    ledger = Ledger.in_memory("task-1")
    spec = TaskSpec(
        task_id="task-1",
        objective="add multiply",
        workspace=".",
        obligations=(
            Obligation(
                id="mul-created",
                kind=PropositionKind.FILE_CREATED,
                target="calc/mul.py",
            ),
        ),
    )
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
        audit_provider=provider,
    )
    result = runtime.run(
        spec,
        ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.EDIT)),
                Finish("done"),
            ]
        ),
    )
    return ledger, result


def test_audit_provider_failure_is_recorded_not_a_verdict() -> None:
    ledger, result = _audit_provider_result(RaisingAuditProvider())
    assert result.accepted is False
    assert any(
        event.kind is EventKind.AUDIT_PROPOSED
        and event.payload.get("status") == "error"
        for event in ledger.read()
    )
    assert any(item.obligation.id == "mul-created" for item in result.debt)


def test_audit_provider_none_is_not_an_audit() -> None:
    ledger, result = _audit_provider_result(NoneAuditProvider())
    assert result.accepted is False
    assert not ledger.read(EventKind.AUDIT_PROPOSED)
    assert any(item.obligation.id == "mul-created" for item in result.debt)


def test_audit_provider_invalid_type_is_recorded() -> None:
    ledger, result = _audit_provider_result(InvalidAuditProvider())
    assert result.accepted is False
    event = next(iter(ledger.read(EventKind.AUDIT_PROPOSED)))
    assert event.payload.get("status") == "error"
    assert "non-AuditProposal" in event.payload.get("reason", "")
