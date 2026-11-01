"""Accepting an unverified gap (#287), without letting it hide failures.

The rule this file exists to protect:

- **no evidence** (abstained/unresolved) → the operator may accept the gap and
  the run may deliver, with ``evidence=PARTIAL`` and a ``debt_accepted`` event;
- **evidence of failure** (a violated verdict) → never acceptable, whatever the
  operator says, because that would turn consent into a way to ship breakage.
"""

from __future__ import annotations

from tests.kernel.test_north_star import ScriptedStrategy, _runtime
from xenon.kernel.contracts import (
    ControlDecisionKind,
    Debt,
    EvidenceStatus,
    Obligation,
    ObligationSource,
    PropositionKind,
    TaskSpec,
    Verdict,
    VerdictStatus,
    VerificationStage,
)
from xenon.kernel.controller import VerificationController
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


def _obligation(oid: str = "task") -> Obligation:
    return Obligation(
        id=oid,
        kind=PropositionKind.REQUIREMENT_MET,
        source=ObligationSource.USER,
        required=True,
        since_seq=0,
    )


def _debt(*, acceptable: bool, accepted: bool, oid: str = "task") -> Debt:
    return Debt(
        obligation=_obligation(oid),
        reason="semantic claim needs a coverage binder",
        acceptable=acceptable,
        user_accepted=accepted,
    )


# ── controller: the decision rule ─────────────────────────────────────


def test_unaccepted_gap_blocks():
    decision = VerificationController().decide((), [_debt(acceptable=True, accepted=False)])

    assert decision.kind is ControlDecisionKind.BLOCK
    assert decision.obligation_ids == ("task",)


def test_accepted_gap_delivers_with_a_reason():
    decision = VerificationController().decide((), [_debt(acceptable=True, accepted=True)])

    assert decision.kind is ControlDecisionKind.DELIVER
    assert "accepted" in decision.reason


def test_a_violated_verdict_blocks_even_when_debt_is_accepted():
    violated = Verdict(
        obligation_id="task",
        stage=VerificationStage.V0,
        status=VerdictStatus.VIOLATED,
        reason="pytest failed",
    )

    decision = VerificationController().decide(
        (violated,), [_debt(acceptable=False, accepted=True)]
    )

    assert decision.kind is ControlDecisionKind.BLOCK
    assert "pytest failed" in decision.reason


def test_mixed_debt_blocks_on_the_unaccepted_part():
    decision = VerificationController().decide(
        (),
        [
            _debt(acceptable=True, accepted=True, oid="a"),
            _debt(acceptable=True, accepted=False, oid="b"),
        ],
    )

    assert decision.kind is ControlDecisionKind.BLOCK
    assert decision.obligation_ids == ("b",)


# ── runtime: the opt-in is recorded, not assumed ─────────────────────


def _spec(**kwargs) -> TaskSpec:
    base = dict(task_id="task-1", objective="inspect", workspace=".")
    base.update(kwargs)
    return TaskSpec(**base)


def test_without_the_opt_in_a_gap_blocks_the_run():
    ledger = Ledger.in_memory("task-1")
    runtime = _runtime(ledger)

    # A plan step that never gets verified leaves a gap.
    from xenon.kernel.contracts import CallTool, Capability, ToolIntent

    result = runtime.run(
        _spec(),
        ScriptedStrategy([CallTool(ToolIntent(capability=Capability.READ))]),
    )

    assert result.accepted is False
    accepted_events = [
        e for e in ledger.read() if e.kind is EventKind.DEBT_ACCEPTED
    ]
    assert accepted_events == [], "no opt-in means no acceptance record"


def test_the_opt_in_is_written_to_the_ledger_when_a_gap_appears():
    ledger = Ledger.in_memory("task-1")
    runtime = _runtime(ledger)

    # Force a gap by handing the runtime an unverifiable verdict.
    runtime._verdicts.append(
        Verdict(
            obligation_id="finalizer",
            stage=VerificationStage.V2,
            status=VerdictStatus.VIOLATED,
            reason="cannot read git status",
        )
    )
    runtime._sync_debt(tuple(runtime._verdicts))

    debt = runtime._debt[0] if runtime._debt else None
    assert debt is not None
    # A violated verdict is never acceptable, so no acceptance event.
    assert debt.acceptable is False
    assert [e for e in ledger.read() if e.kind is EventKind.DEBT_ACCEPTED] == []


def test_an_abstained_gap_is_acceptable_and_recorded():
    ledger = Ledger.in_memory("task-1")
    runtime = _runtime(ledger)
    runtime._accept_debt = True

    runtime._verdicts.append(
        Verdict(
            obligation_id="semantic",
            stage=VerificationStage.V2,
            status=VerdictStatus.UNVERIFIABLE,
            reason="needs a coverage binder",
        )
    )
    runtime._sync_debt(tuple(runtime._verdicts))

    assert runtime._debt and runtime._debt[0].user_accepted is True
    accepted = [e for e in ledger.read() if e.kind is EventKind.DEBT_ACCEPTED]
    assert len(accepted) == 1
    assert accepted[0].payload["obligation_id"] == "semantic"
    assert accepted[0].payload["accepted_by"] == "user"


def test_evidence_never_reads_verified_while_debt_is_open():
    ledger = Ledger.in_memory("task-1")
    runtime = _runtime(ledger)
    runtime._accept_debt = True
    runtime._verdicts.append(
        Verdict(
            obligation_id="semantic",
            stage=VerificationStage.V2,
            status=VerdictStatus.UNVERIFIABLE,
            reason="needs a coverage binder",
        )
    )
    runtime._sync_debt(tuple(runtime._verdicts))
    runtime._spec = _spec()

    status = runtime._evidence_status(())
    assert status is EvidenceStatus.PARTIAL
    assert status is not EvidenceStatus.VERIFIED


def test_spec_flag_is_off_by_default():
    assert TaskSpec(task_id="t", objective="o", workspace=".").accept_debt is False


def test_the_spec_flag_reaches_the_runtime():
    """A flag that never arrives is the same as no feature at all."""

    from tests.kernel.test_north_star import FakeCatalog, FakeGateway
    from xenon.kernel.controller import VerificationController
    from xenon.kernel.runtime import LoopRuntime

    ledger = Ledger.in_memory("task-1")
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
    )
    runtime.run(_spec(accept_debt=True), ScriptedStrategy([]))

    assert runtime._accept_debt is True


def test_an_unavailable_finalizer_is_unverifiable_not_violated(tmp_path):
    """'Cannot read git status' means we could not verify, not that it failed."""

    from xenon.catalog.finalizer import GitFinalizer

    outcome = GitFinalizer(tmp_path).finalize(
        verified_paths=(), objective="x", task_id="t1"
    )

    assert outcome.accepted is False
    assert outcome.unavailable is True
    assert "git status" in outcome.reason


def test_an_accepted_unverifiable_verdict_no_longer_abstains():
    """Acceptance covers 'no evidence', which includes unverifiable verdicts."""

    unverifiable = Verdict(
        obligation_id="finalizer",
        stage=VerificationStage.V2,
        status=VerdictStatus.UNVERIFIABLE,
        reason="cannot read git status",
    )
    debt = Debt(
        obligation=_obligation("finalizer"),
        reason="cannot read git status",
        acceptable=True,
        user_accepted=True,
    )

    decision = VerificationController().decide((unverifiable,), (debt,))

    assert decision.kind is ControlDecisionKind.DELIVER


def test_an_unaccepted_unverifiable_verdict_still_abstains():
    unverifiable = Verdict(
        obligation_id="finalizer",
        stage=VerificationStage.V2,
        status=VerdictStatus.UNVERIFIABLE,
        reason="cannot read git status",
    )
    debt = Debt(
        obligation=_obligation("finalizer"),
        reason="cannot read git status",
        acceptable=True,
        user_accepted=False,
    )

    decision = VerificationController().decide((unverifiable,), (debt,))

    assert decision.kind is ControlDecisionKind.ABSTAIN
