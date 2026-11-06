"""Nothing asserted is not the same as everything confirmed.

A real run against SmartBench #26 finished with ``evidence=verified, gaps=0``
while its ledger held no edit, two failing test receipts, and ``coverage: []``.
The model had been honest in its answer ("zero tests ran, zero passed"); the
label was the part that lied.  Three separate places let an empty set pass for
a full one:

1. ``VerificationController.audit(None, ...)`` returned SATISFIED when the
   model asserted no claim and carried no debt.
2. The runtime synthesised that verdict even when V2 coverage was empty.
3. ``_evidence_status`` accepted "all satisfied" over a set whose every member
   was satisfied without citing a single fact.

These tests pin the rule they were all violating: an evidence label needs at
least one verdict that resolved against real facts.
"""

from __future__ import annotations

from pathlib import Path

from tests.kernel.test_north_star import ScriptedStrategy, _runtime
from xenon.kernel.contracts import (
    CallTool,
    Capability,
    EvidenceStatus,
    Finish,
    Obligation,
    PropositionKind,
    TaskSpec,
    ToolIntent,
)
from xenon.kernel.controller import VerificationController
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


def _spec(tmp_path: Path, *, obligations: tuple[Obligation, ...] = ()) -> TaskSpec:
    return TaskSpec(
        task_id="no-claim",
        objective="do the thing",
        workspace=str(tmp_path),
        obligations=obligations,
    )


def test_a_run_that_says_done_without_doing_anything_is_not_verified(
    tmp_path: Path,
) -> None:
    ledger = Ledger.in_memory("no-claim")
    result = _runtime(ledger).run(_spec(tmp_path), ScriptedStrategy([Finish("done")]))

    assert result.accepted is True
    assert result.evidence_status is not EvidenceStatus.VERIFIED


def test_a_read_that_satisfies_nothing_is_not_verified(tmp_path: Path) -> None:
    """The SmartBench shape: the model *did* run tools, and the receipts are
    OK.  What it never did is anything the task asked for.  A successful read
    used to be enough to call the whole run verified."""

    ledger = Ledger.in_memory("no-claim")
    strategy = ScriptedStrategy(
        [
            CallTool(ToolIntent(capability=Capability.READ)),
            Finish("I only looked around; nothing was changed."),
        ]
    )

    result = _runtime(ledger).run(_spec(tmp_path), strategy)

    assert result.accepted is True
    assert result.evidence_status is not EvidenceStatus.VERIFIED


def test_an_uncovered_obligation_keeps_the_run_out_of_verified(tmp_path: Path) -> None:
    """The SmartBench case: intake derived no obligations, so V2 ran on an
    empty coverage set and reported success.  With an obligation that no fact
    backs, the same shape must not verify."""

    ledger = Ledger.in_memory("no-claim")
    spec = _spec(
        tmp_path,
        obligations=(
            Obligation(
                id="o1",
                kind=PropositionKind.FILE_CHANGED,
                target="calc.py",
            ),
        ),
    )

    result = _runtime(ledger).run(spec, ScriptedStrategy([Finish("done")]))

    assert result.accepted is True
    assert result.evidence_status is not EvidenceStatus.VERIFIED
    final_audits = [
        event.payload
        for event in ledger.read(EventKind.AUDIT_REPORTED)
        if event.payload.get("scope") == "final"
    ]
    assert final_audits
    unbacked = [
        row
        for row in final_audits[0]["coverage"]
        if row["status"] == "satisfied" and not row["fact_ids"]
    ]
    assert not unbacked


def test_a_satisfied_verdict_without_facts_does_not_count_as_evidence() -> None:
    from xenon.kernel.contracts import (
        TaskMode,
        Verdict,
        VerdictStatus,
        VerificationStage,
    )
    from xenon.kernel.runtime import LoopRuntime

    runtime = LoopRuntime.__new__(LoopRuntime)
    runtime._spec = TaskSpec(
        task_id="t", objective="o", workspace=".", mode=TaskMode.TASK
    )
    runtime._debt = []
    runtime._ledger = None

    hollow = Verdict(
        obligation_id="task",
        stage=VerificationStage.V2,
        status=VerdictStatus.SATISFIED,
    )

    assert runtime._evidence_status((hollow,)) is not EvidenceStatus.VERIFIED
    assert runtime._evidence_status(()) is not EvidenceStatus.VERIFIED


def test_audit_without_a_claim_is_unverifiable_not_satisfied() -> None:
    from xenon.kernel.contracts import VerdictStatus

    verdict = VerificationController().audit(None, Ledger.in_memory("t"), ())

    assert verdict.status is VerdictStatus.UNVERIFIABLE
    assert "verifiable" in verdict.reason or "claim" in verdict.reason
