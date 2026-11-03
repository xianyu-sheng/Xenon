"""Lesson proposer: harness-judged failures become proposals (slice 2).

Guardrail 1: proposals come only from deterministic events with template
action text — never the model's prose.  The runtime writes LESSON_PROPOSED at
the failure point; this file pins the proposer and the runtime wiring.
"""

from __future__ import annotations

from xenon.kernel.lesson_proposer import propose_lesson

# ── the proposer ──────────────────────────────────────────────────


def test_a_veto_proposes_a_pitfall() -> None:
    proposal = propose_lesson(
        "policy_decision",
        {"kind": "veto", "capability": "shell", "reason": "destructive command"},
        source_task="t1",
    )

    assert proposal is not None
    assert proposal.kind == "pitfall"
    assert "destructive command" in proposal.action_text
    assert proposal.source_task == "t1"


def test_a_non_veto_policy_decision_proposes_nothing() -> None:
    assert propose_lesson("policy_decision", {"kind": "allow"}) is None


def test_a_debt_proposes_a_pitfall() -> None:
    proposal = propose_lesson(
        "debt_opened",
        {"kind": "requirement_met", "target": "x.py", "reason": "no receipt"},
    )

    assert proposal is not None
    assert "x.py" in proposal.action_text
    assert "回执" in proposal.action_text


def test_a_loop_signal_proposes_a_parameter() -> None:
    proposal = propose_lesson(
        "loop_detected", {"signal": "identical_call", "reason": "same call x5"}
    )

    assert proposal is not None
    assert proposal.kind == "parameter"
    assert "identical_call" in proposal.action_text


def test_a_denied_approval_proposes_a_preference() -> None:
    proposal = propose_lesson(
        "user_approval", {"approved": False, "tool": "shell", "reason": "no"}
    )

    assert proposal is not None
    assert proposal.kind == "preference"


def test_an_approved_call_proposes_nothing() -> None:
    assert propose_lesson("user_approval", {"approved": True}) is None


def test_unknown_events_propose_nothing() -> None:
    assert propose_lesson("tool_receipt", {"status": "ok"}) is None


def test_proposals_are_fingerprinted_deterministically() -> None:
    a = propose_lesson("debt_opened", {"kind": "x", "reason": "r"})
    b = propose_lesson("debt_opened", {"kind": "x", "reason": "r"})

    assert a is not None and b is not None
    assert a.fingerprint == b.fingerprint


# ── the runtime wiring ────────────────────────────────────────────


def test_the_runtime_writes_lesson_proposed_on_debt(tmp_path) -> None:
    from tests.kernel.test_north_star import ScriptedStrategy, _runtime
    from tests.kernel.test_session_grants import _policy
    from xenon.kernel.contracts import CallTool, Capability, TaskSpec, ToolIntent
    from xenon.ledger.store import Ledger

    ledger = Ledger.in_memory("lesson-debt")
    runtime = _runtime(ledger, policies=(_policy(str(tmp_path)),))
    # A shell call whose claimed effect can never resolve opens debt.
    action = CallTool(
        ToolIntent(capability=Capability.SHELL, params={"command": "echo hi"})
    )
    runtime.run(
        TaskSpec(task_id="lesson-debt", objective="o", workspace=str(tmp_path)),
        ScriptedStrategy([action]),
    )

    kinds = [event.kind.value for event in ledger.read()]
    assert "lesson_proposed" in kinds


def test_the_runtime_writes_lesson_proposed_on_a_denied_approval(tmp_path) -> None:
    from tests.kernel.test_north_star import ScriptedStrategy, _runtime
    from tests.kernel.test_session_grants import _policy
    from xenon.kernel.contracts import CallTool, Capability, TaskSpec, ToolIntent
    from xenon.ledger.store import Ledger

    ledger = Ledger.in_memory("lesson-deny")
    runtime = _runtime(ledger, policies=(_policy(str(tmp_path)),))
    runtime._approval = _DenyApproval()

    action = CallTool(
        ToolIntent(capability=Capability.SHELL, params={"command": "git log"})
    )
    runtime.run(
        TaskSpec(task_id="lesson-deny", objective="o", workspace=str(tmp_path)),
        ScriptedStrategy([action]),
    )

    kinds = [event.kind.value for event in ledger.read()]
    assert "user_approval" in kinds
    assert "lesson_proposed" in kinds


class _DenyApproval:
    last_reason = "user said no"
    last_scope = ""

    def request(self, view, action, decision) -> bool:
        return False


def test_a_broken_proposer_never_fails_the_task(tmp_path) -> None:
    import xenon.kernel.runtime as runtime_module
    from tests.kernel.test_north_star import ScriptedStrategy, _runtime
    from tests.kernel.test_session_grants import _policy
    from xenon.kernel.contracts import CallTool, Capability, TaskSpec, ToolIntent
    from xenon.ledger.store import Ledger

    ledger = Ledger.in_memory("lesson-broken")
    runtime = _runtime(ledger, policies=(_policy(str(tmp_path)),))
    runtime._approval = _DenyApproval()

    def boom(*args, **kwargs):
        raise RuntimeError("proposer broke")

    runtime_module.propose_lesson = boom
    action = CallTool(
        ToolIntent(capability=Capability.SHELL, params={"command": "git log"})
    )
    runtime.run(
        TaskSpec(task_id="lesson-broken", objective="o", workspace=str(tmp_path)),
        ScriptedStrategy([action]),
    )

    # The task ran (blocked by the denial), and the broken proposer did not
    # turn it into an exception.
    assert runtime_module.propose_lesson is boom
