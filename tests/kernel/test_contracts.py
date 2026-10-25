"""Contract tests for the kernel types.

These tests are behavior-free: they freeze the vocabulary and the phase table
before the runtime lands.
"""

from __future__ import annotations

import pytest

from xenon.kernel.contracts import (
    ActionKind,
    Capability,
    Claim,
    ClaimSource,
    EffectKind,
    FsWrite,
    PropositionKind,
    ReceiptStatus,
    Risk,
    StepContract,
    StepKind,
    TaskPhase,
    ToolCall,
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
    assert isinstance(ToolCall(spec_name="mutate"), ToolCall)
