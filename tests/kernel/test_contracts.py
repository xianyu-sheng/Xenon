"""Contract tests for the kernel types.

These tests are behavior-free: they freeze the vocabulary and the phase table
before the runtime lands.
"""

from __future__ import annotations

import pytest

from xenon.kernel.contracts import (
    ActionKind,
    BindingStatus,
    Budget,
    Capability,
    Claim,
    ClaimSource,
    Effect,
    EffectKind,
    EvidenceStatus,
    FsDelete,
    FsWrite,
    ModelView,
    PolicyDecision,
    PolicyDecisionKind,
    PropositionKind,
    ReceiptStatus,
    Resolution,
    ResolutionStatus,
    Risk,
    StepContract,
    StepKind,
    TaskPhase,
    TaskResult,
    TaskSpec,
    TestRun,
    ToolBinding,
    ToolIntent,
    ToolReceipt,
    ToolSpec,
    VerificationStage,
)
from xenon.kernel.phases import (
    LEGAL_TRANSITIONS,
    TERMINAL_PHASES,
    assert_transition,
    is_legal_transition,
)


def test_capability_set_is_closed() -> None:
    assert {item.value for item in Capability} == {
        "read",
        "search",
        "edit",
        "verify",
        "vcs",
        "shell",
        "remote",
    }


def test_effect_kinds_are_stable() -> None:
    assert {item.value for item in EffectKind} == {
        "fs_write",
        "fs_delete",
        "test_run",
        "vcs_commit",
        "vcs_push",
        "read",
        "network",
        "opaque",
    }


def test_verification_stages_are_v0_v1_v2() -> None:
    assert [item.value for item in VerificationStage] == ["v0", "v1", "v2"]


def test_terminal_phases_have_no_outgoing_edges() -> None:
    for phase in TERMINAL_PHASES:
        assert LEGAL_TRANSITIONS[phase] == frozenset()


def test_every_non_terminal_phase_can_reach_a_terminal_phase() -> None:
    def reaches_terminal(start: TaskPhase) -> bool:
        seen: set[TaskPhase] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in TERMINAL_PHASES:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(LEGAL_TRANSITIONS.get(current, frozenset()))
        return False

    for phase in TaskPhase:
        assert reaches_terminal(phase), phase


def test_illegal_transition_is_rejected() -> None:
    assert is_legal_transition(TaskPhase.CREATED, TaskPhase.INTAKE)
    assert not is_legal_transition(TaskPhase.CREATED, TaskPhase.DELIVERED)
    with pytest.raises(ValueError, match="illegal task transition"):
        assert_transition(TaskPhase.CREATED, TaskPhase.DELIVERED)


def test_action_kinds_are_stable() -> None:
    assert {item.value for item in ActionKind} == {
        "think",
        "call_tool",
        "submit_plan",
        "update_plan",
        "request_verification",
        "ask_user",
        "finish",
        "abort",
    }


def test_receipt_carries_declared_and_observed_effects() -> None:
    receipt = ToolReceipt(
        receipt_id="r1",
        call_id="c1",
        task_id="t1",
        tool="mutate",
        status=ReceiptStatus.OK,
        effects=(FsWrite(paths=("a.py",), hashes={"a.py": "h"}),),
        declared_effects=(EffectKind.FS_WRITE,),
        observed_effects=(EffectKind.FS_WRITE,),
    )
    assert receipt.effects[0].kind is EffectKind.FS_WRITE
    assert receipt.declared_effects == receipt.observed_effects


def test_spec_and_intent_are_capability_based() -> None:
    spec = ToolSpec(
        name="mutate",
        pack="core",
        capabilities=frozenset({Capability.EDIT}),
        effects=frozenset({EffectKind.FS_WRITE}),
        risk=Risk.WRITE,
    )
    intent = ToolIntent(capability=Capability.EDIT, targets=("a.py",))
    assert Capability.EDIT in spec.capabilities
    assert intent.capability is Capability.EDIT


def test_step_contract_uses_intent_not_tool_name() -> None:
    step = StepContract(
        id=1,
        kind=StepKind.EDIT,
        intent=ToolIntent(capability=Capability.EDIT),
        targets=("a.py",),
        prefer="mutate",
    )
    assert step.intent is not None
    assert step.intent.capability is Capability.EDIT
    assert step.prefer == "mutate"


def test_claim_is_a_proposal_not_a_fact() -> None:
    claim = Claim(
        id="claim-1",
        kind=PropositionKind.FILE_CHANGED,
        target="a.py",
        source=ClaimSource.LLM,
    )
    assert claim.source is ClaimSource.LLM
    assert not hasattr(claim, "fact_ids")


def test_tool_binding_reports_ambiguity_explicitly() -> None:
    ambiguous = ToolBinding(
        status=BindingStatus.AMBIGUOUS,
        candidates=("mutate", "rewrite"),
    )
    assert ambiguous.call is None
    assert ambiguous.candidates == ("mutate", "rewrite")
    assert BindingStatus.BOUND.value == "bound"


def test_effect_union_is_runtime_checkable() -> None:
    effect = FsWrite(paths=("a.py",))
    assert isinstance(effect, Effect)


def test_task_result_fails_closed_on_evidence_status() -> None:
    result = TaskResult(task_id="t1", accepted=True, phase=TaskPhase.FINALIZED)
    assert result.evidence_status is EvidenceStatus.UNVERIFIED
    verified = TaskResult(
        task_id="t2",
        accepted=True,
        phase=TaskPhase.FINALIZED,
        evidence_status=EvidenceStatus.VERIFIED,
    )
    assert verified.evidence_status is EvidenceStatus.VERIFIED


def test_fs_write_distinguishes_noop_and_created_paths() -> None:
    write = FsWrite(paths=("a.py",), created_paths=("a.py",), changed=False)
    assert write.changed is False
    assert write.created_paths == ("a.py",)


def test_test_run_reports_skipped_separately() -> None:
    run = TestRun(target="tests/test_a.py", tests_run=3, passed=1, skipped=2)
    assert run.skipped == 2
    assert run.passed == 1


def test_task_result_carries_claim_resolutions_and_view_phase() -> None:
    resolution = Resolution(
        claim_id="claim-1",
        status=ResolutionStatus.BOUND,
        fact_ids=("fact-1",),
    )
    result = TaskResult(
        task_id="t1",
        accepted=True,
        phase=TaskPhase.FINALIZED,
        resolutions=(resolution,),
    )
    assert result.resolutions[0].status is ResolutionStatus.BOUND

    view = ModelView(
        task=TaskSpec(task_id="t1", objective="x", workspace="."),
        system="",
        tools=(),
        working_set=(),
        debt=(),
        budget=Budget(),
        input_seq=1,
        hash="h",
    )
    assert view.phase is TaskPhase.RUNNING


def test_fs_delete_and_policy_decision_carry_explicit_scope() -> None:
    delete = FsDelete(paths=("a.py",), changed=False)
    assert delete.changed is False
    decision = PolicyDecision(
        kind=PolicyDecisionKind.REQUIRE_APPROVAL,
        reason="write scope",
        scope=("a.py",),
        ttl_seconds=30.0,
    )
    assert decision.scope == ("a.py",)
    assert decision.ttl_seconds == 30.0
