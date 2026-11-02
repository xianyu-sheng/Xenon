"""V2 coverage proposer (#286): proposals point at facts, never decide.

The rule this file protects: a proposal can *deliver* a task by pointing at
receipts that exist, but it must never make the evidence read VERIFIED —
"these facts prove this requirement" is a judgement, and the harness does not
launder judgements into proof.
"""

from __future__ import annotations

import xenon.kernel  # noqa: F401  — breaks the ledger<->kernel import cycle
from xenon.harness.llm import (
    LLMAuditProposer,
    ModelConfig,
    _audit_proposal,
    build_audit_messages,
)
from xenon.kernel.contracts import (
    AuditProposal,
    EvidenceStatus,
    Obligation,
    ObligationSource,
    PropositionKind,
    Verdict,
    VerdictStatus,
    VerificationStage,
)
from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.store import Ledger


class _Client:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, model_id, messages, **kwargs):
        self.calls.append(messages)
        return self.reply


def _obligation(oid: str = "o1") -> Obligation:
    return Obligation(
        id=oid,
        kind=PropositionKind.REQUIREMENT_MET,
        source=ObligationSource.USER,
        required=True,
        since_seq=0,
    )


def _ledger_with_write(tmp_path) -> Ledger:
    ledger = Ledger(tmp_path / "t.jsonl", "t")
    ledger.append(
        EventKind.TOOL_RECEIPT,
        EventSource.TOOL,
        {
            "receipt": {
                "receipt_id": "r1",
                "call_id": "c1",
                "task_id": "t",
                "tool": "apply_patch",
                "status": "ok",
                "effects": [
                    {"type": "fs_write", "paths": ["calc.py"], "changed": True}
                ],
                "declared_effects": ["fs_write"],
                "observed_effects": ["fs_write"],
            },
            "fact_ids": ["f1"],
        },
    )
    return ledger


# ── parsing: fail closed ─────────────────────────────────────────────


def test_a_coverage_entry_needs_a_target_or_a_test():
    assert _audit_proposal({"coverage": [{"obligation_id": "o1"}]}) is None


def test_an_entry_without_an_obligation_id_is_dropped():
    assert _audit_proposal({"coverage": [{"artifact_paths": ["a.py"]}]}) is None


def test_a_gap_is_kept_even_without_coverage():
    proposal = _audit_proposal({"gaps": [{"obligation_id": "o1", "reason": "no test"}]})

    assert proposal is not None and proposal.coverage == ()
    assert proposal.gaps[0].reason == "no test"


def test_a_well_formed_proposal_is_parsed():
    proposal = _audit_proposal(
        {
            "coverage": [
                {
                    "obligation_id": "o1",
                    "artifact_paths": ["calc.py"],
                    "test_targets": ["tests/test_calc.py"],
                    "rationale": "the fix plus its test",
                }
            ]
        }
    )

    assert isinstance(proposal, AuditProposal)
    assert proposal.coverage[0].artifact_paths == ("calc.py",)


# ── provider behaviour ───────────────────────────────────────────────


def test_a_broken_provider_reply_yields_no_proposal(tmp_path):
    ledger = _ledger_with_write(tmp_path)
    provider = LLMAuditProposer(
        ModelConfig(model_id="test/fake"), client=_Client("not json at all")
    )

    assert provider.propose((_obligation(),), ledger) is None


def test_a_provider_exception_yields_no_proposal(tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError("model down")

    provider = LLMAuditProposer(ModelConfig(model_id="test/fake"), client=boom)

    assert provider.propose((_obligation(),), _ledger_with_write(tmp_path)) is None


def test_no_obligations_means_no_model_call(tmp_path):
    client = _Client("{}")
    provider = LLMAuditProposer(ModelConfig(model_id="test/fake"), client=client)

    assert provider.propose((), _ledger_with_write(tmp_path)) is None
    assert client.calls == []


def test_the_prompt_only_shows_receipts_the_model_may_point_at(tmp_path):
    ledger = _ledger_with_write(tmp_path)

    messages = build_audit_messages((_obligation(),), ledger)
    user = messages[-1]["content"]

    assert "calc.py" in user
    assert "do NOT decide" in messages[0]["content"].lower() or "not decide" in (
        messages[0]["content"].lower()
    )


# ── provenance: proposals never upgrade evidence ─────────────────────


def test_a_proposal_backed_verdict_is_flagged():
    from xenon.kernel.audit import _verdict
    from xenon.kernel.contracts import CoverageRow, ResolutionStatus

    proposal_row = CoverageRow(
        obligation_id="o1",
        kind=PropositionKind.REQUIREMENT_MET,
        target="",
        status=ResolutionStatus.BOUND,
        fact_ids=("f1",),
        reason="proposal matched deterministic facts",
        source="proposal",
    )
    deterministic_row = CoverageRow(
        obligation_id="o2",
        kind=PropositionKind.FILE_CHANGED,
        target="calc.py",
        status=ResolutionStatus.BOUND,
        fact_ids=("f1",),
        reason="bound",
        source="binder",
    )

    assert _verdict(proposal_row).from_proposal is True
    assert _verdict(deterministic_row).from_proposal is False


def test_proposal_backed_evidence_is_capped_at_partial():
    from xenon.kernel.contracts import TaskMode, TaskSpec
    from xenon.kernel.runtime import LoopRuntime
    from xenon.ledger.store import Ledger as _L

    runtime = LoopRuntime.__new__(LoopRuntime)
    runtime._spec = TaskSpec(
        task_id="t", objective="o", workspace=".", mode=TaskMode.TASK
    )
    runtime._debt = []
    del _L  # not needed: the status helper only looks at verdicts

    proposal_verdict = Verdict(
        obligation_id="o1",
        stage=VerificationStage.V2,
        status=VerdictStatus.SATISFIED,
        fact_ids=("f1",),
        from_proposal=True,
    )
    deterministic = Verdict(
        obligation_id="o2",
        stage=VerificationStage.V2,
        status=VerdictStatus.SATISFIED,
        fact_ids=("f1",),
    )

    assert runtime._evidence_status((deterministic,)) is EvidenceStatus.VERIFIED
    assert runtime._evidence_status((proposal_verdict,)) is EvidenceStatus.PARTIAL
