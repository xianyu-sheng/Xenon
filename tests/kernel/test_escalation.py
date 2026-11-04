"""Turn-level capability escalation (#392).

A read-level turn maps to a read-only policy, so a task that genuinely needs the
network or a command used to simply block — and the model had to invent a
boundary to explain why (#165). ``request_escalation`` is the formal request:
the harness asks the human through the existing approval panel, an approval
becomes a one-shot grant, and a refusal blocks the task.

Acceptance, one test each:

1. approval path: a granted escalation lets the *next* call run;
2. refusal: blocked with the capability and the human's answer in the reason;
3. no approver (non-TTY): blocked immediately, reason is actionable;
4. hard rules are refused **without** prompting (push / destructive);
5. the grant is one-shot: the following identical call is refused again.
"""

from __future__ import annotations

from typing import Any

from tests.kernel.test_north_star import ScriptedStrategy, _runtime
from xenon.kernel.contracts import (
    CallTool,
    Capability,
    ReceiptStatus,
    RequestEscalation,
    TaskPhase,
    TaskSpec,
    ToolIntent,
    ToolReceipt,
)
from xenon.kernel.policies import PermissionMode, PermissionPolicy
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


class _Yes:
    """An approver that always says yes, and records what it was asked."""

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.asked: list[tuple[str, dict]] = []
        self.last_reason = ""
        self.last_scope = ""

    def request(self, view: Any, action: Any, decision: Any) -> bool:
        self.asked.append(
            (action.intent.capability.value, dict(action.intent.params))
        )
        self.last_scope = "once"
        self.last_reason = "" if self.answer else "user rejected"
        return self.answer


class _Gateway:
    """Records every call the gateway was actually asked to execute."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.ok_after = 0

    def execute(self, call: Any, services: Any) -> ToolReceipt:
        self.calls.append(call.spec_name)
        if len(self.calls) <= self.ok_after:
            return ToolReceipt(
                receipt_id=f"r-{len(self.calls)}",
                call_id=f"c-{len(self.calls)}",
                task_id="t",
                tool=call.spec_name,
                status=ReceiptStatus.FAILED,
                error="denied by test gateway",
            )
        return ToolReceipt(
            receipt_id=f"r-{len(self.calls)}",
            call_id=f"c-{len(self.calls)}",
            task_id="t",
            tool=call.spec_name,
            status=ReceiptStatus.OK,
        )


class _ShellCatalog:
    """A catalog with a real shell tool: the granted call must reach a gateway."""

    def specs(self):
        from xenon.kernel.contracts import EffectKind, Risk, ToolSpec

        return (
            ToolSpec(
                name="run_shell",
                pack="fake",
                capabilities=frozenset({Capability.SHELL}),
                effects=frozenset({EffectKind.OPAQUE}),
                risk=Risk.SENSITIVE,
            ),
            ToolSpec(
                name="inspect",
                pack="fake",
                capabilities=frozenset({Capability.READ}),
                effects=frozenset({EffectKind.READ}),
                risk=Risk.INFO,
            ),
        )

    def bind(self, intent):
        from xenon.kernel.contracts import BindingStatus, ToolBinding, ToolCall

        name = "run_shell" if intent.capability is Capability.SHELL else "inspect"
        return ToolBinding(
            status=BindingStatus.BOUND,
            call=ToolCall(spec_name=name, params=dict(intent.params)),
        )


def _shell_runtime(ledger, policy, approver=None, gateway=None):
    from xenon.kernel.controller import VerificationController
    from xenon.kernel.runtime import LoopRuntime

    runtime = LoopRuntime(
        catalog=_ShellCatalog(),
        gateway=gateway or _Gateway(),
        controller=VerificationController(),
        ledger=ledger,
        policies=(policy,),
        approval=approver,
    )
    return runtime


def _spec(task_id: str) -> TaskSpec:
    return TaskSpec(task_id=task_id, objective="o", workspace=".")


def _escalate(command: str, reason: str = "need the network") -> RequestEscalation:
    return RequestEscalation(
        ToolIntent(capability=Capability.SHELL, params={"command": command}),
        reason,
    )


def _shell(command: str) -> CallTool:
    return CallTool(
        ToolIntent(capability=Capability.SHELL, params={"command": command})
    )


def _read_only(tmp_path) -> PermissionPolicy:
    return PermissionPolicy(
        mode=PermissionMode.READ_ONLY, workspace=str(tmp_path), allow_approval=True
    )


def _decisions(ledger) -> list[dict]:
    return [
        event.payload
        for event in ledger.read()
        if event.kind is EventKind.POLICY_DECISION
    ]


# ── 1. an approved escalation unblocks the call ───────────────────


def test_an_approved_escalation_lets_the_next_call_run(tmp_path) -> None:
    ledger = Ledger.in_memory("esc-ok")
    approver = _Yes(True)
    gateway = _Gateway()
    runtime = _shell_runtime(ledger, _read_only(tmp_path), approver, gateway)

    result = runtime.run(
        _spec("esc-ok"),
        ScriptedStrategy([_escalate("curl -s wttr.in"), _shell("curl -s wttr.in")]),
    )

    # The human was asked exactly once, for the exact command.
    assert approver.asked == [("shell", {"command": "curl -s wttr.in"})]
    # The call actually reached the gateway after the grant.
    assert gateway.calls, "the granted call must execute"
    # The decision is in the ledger, so a replay can explain it.
    assert any(item.get("escalation") for item in _decisions(ledger) + [
        event.payload
        for event in ledger.read()
        if event.kind is EventKind.USER_APPROVAL
    ])
    assert result.phase is not TaskPhase.BLOCKED


# ── 2. a refusal blocks with the capability and the answer ────────


def test_a_refused_escalation_blocks_with_the_reason(tmp_path) -> None:
    ledger = Ledger.in_memory("esc-no")
    approver = _Yes(False)
    runtime = _runtime(ledger, policies=(_read_only(tmp_path),))
    runtime._approval = approver

    result = runtime.run(_spec("esc-no"), ScriptedStrategy([_escalate("curl x")]))

    assert result.accepted is False
    assert result.phase is TaskPhase.BLOCKED
    approvals = [
        event.payload
        for event in ledger.read()
        if event.kind is EventKind.USER_APPROVAL
    ]
    assert approvals and approvals[0]["approved"] is False
    assert approvals[0]["escalation"] is True
    reasons = " ".join(str(item.get("reason")) for item in _decisions(ledger))
    assert "shell" in reasons and "user rejected" in reasons


# ── 3. no approver: fail closed, actionable ──────────────────────


def test_no_approver_fails_closed_with_an_actionable_reason(tmp_path) -> None:
    ledger = Ledger.in_memory("esc-notty")
    gateway = _Gateway()
    runtime = _shell_runtime(ledger, _read_only(tmp_path), None, gateway)

    result = runtime.run(_spec("esc-notty"), ScriptedStrategy([_escalate("curl x")]))

    assert result.accepted is False
    assert gateway.calls == [], "nothing may execute without an approver"
    reasons = " ".join(str(item.get("reason")) for item in _decisions(ledger))
    assert "no interactive terminal" in reasons
    assert "--permission bypass" in reasons


# ── 4. hard rules are refused without prompting ──────────────────


def test_push_escalation_is_refused_without_prompting(tmp_path) -> None:
    ledger = Ledger.in_memory("esc-push")
    approver = _Yes(True)
    runtime = _runtime(ledger, policies=(_read_only(tmp_path),))
    runtime._approval = approver

    result = runtime.run(
        _spec("esc-push"), ScriptedStrategy([_escalate("git push origin main")])
    )

    assert result.accepted is False
    assert approver.asked == [], "push must never reach the human"
    reasons = " ".join(str(item.get("reason")) for item in _decisions(ledger))
    assert "push is never approvable" in reasons


def test_destructive_escalation_is_refused_without_prompting(tmp_path) -> None:
    ledger = Ledger.in_memory("esc-rm")
    approver = _Yes(True)
    runtime = _runtime(ledger, policies=(_read_only(tmp_path),))
    runtime._approval = approver

    result = runtime.run(
        _spec("esc-rm"), ScriptedStrategy([_escalate("rm -rf /tmp/x")])
    )

    assert result.accepted is False
    assert approver.asked == []
    reasons = " ".join(str(item.get("reason")) for item in _decisions(ledger))
    assert "destructive command is never approvable" in reasons


def test_plan_only_never_asks(tmp_path) -> None:
    """A plan turn must stay side-effect free: the loop refuses any non-plan
    action before dispatch, so escalation cannot even reach the approver."""
    from xenon.kernel.contracts import TaskMode

    ledger = Ledger.in_memory("esc-plan")
    approver = _Yes(True)
    runtime = _runtime(ledger, policies=(_read_only(tmp_path),))
    runtime._approval = approver

    result = runtime.run(
        TaskSpec(
            task_id="esc-plan",
            objective="plan only",
            workspace=str(tmp_path),
            mode=TaskMode.PLAN,
        ),
        ScriptedStrategy([_escalate("curl x")]),
    )

    assert result.accepted is False
    assert approver.asked == [], "plan mode must not prompt for execution"
    transitions = " ".join(
        str(event.payload.get("reason"))
        for event in ledger.read()
        if event.kind is EventKind.STATE_TRANSITION
    )
    assert "plan-only task requires a plan proposal" in transitions


# ── 5. the grant is one-shot ─────────────────────────────────────


def test_the_grant_is_consumed_by_exactly_one_call(tmp_path) -> None:
    ledger = Ledger.in_memory("esc-once")
    approver = _Yes(True)
    gateway = _Gateway()
    runtime = _shell_runtime(ledger, _read_only(tmp_path), approver, gateway)

    result = runtime.run(
        _spec("esc-once"),
        ScriptedStrategy(
            [
                _escalate("curl -s wttr.in"),
                _shell("curl -s wttr.in"),  # granted: runs
                _shell("curl -s wttr.in"),  # grant already revoked: refused
            ]
        ),
    )

    assert len(gateway.calls) == 1, "the second identical call must be refused"
    assert len(approver.asked) == 1, "and it must not silently re-prompt"
    reasons = " ".join(str(item.get("reason")) for item in _decisions(ledger))
    assert "read-only mode" in reasons
    assert result.phase is TaskPhase.BLOCKED


# ── parser + failure paths (each one fail-closed) ────────────────


def test_the_action_kind_is_exposed() -> None:
    from xenon.kernel.contracts import ActionKind

    assert _escalate("curl x").kind is ActionKind.REQUEST_ESCALATION


def test_parser_accepts_request_escalation() -> None:
    from xenon.harness.llm import parse_action

    action = parse_action(
        {
            "action": "request_escalation",
            "capability": "shell",
            "params": {"command": "curl -s wttr.in"},
            "reason": "live weather",
        }
    )
    assert isinstance(action, RequestEscalation)
    assert action.intent.capability is Capability.SHELL
    assert action.reason == "live weather"


def test_parser_rejects_an_unknown_capability() -> None:
    import pytest

    from xenon.harness.llm import ActionParseError, parse_action

    with pytest.raises(ActionParseError):
        parse_action(
            {"action": "request_escalation", "capability": "telepathy", "reason": "r"}
        )


def test_parser_requires_a_reason() -> None:
    import pytest

    from xenon.harness.llm import ActionParseError, parse_action

    with pytest.raises(ActionParseError, match="reason is required"):
        parse_action({"action": "request_escalation", "capability": "shell"})


def test_a_policy_that_cannot_evaluate_fails_closed(tmp_path) -> None:
    ledger = Ledger.in_memory("esc-broken")
    approver = _Yes(True)

    class Broken(PermissionPolicy):
        def refuse_escalation(self, intent):
            raise RuntimeError("boom")

    policy = Broken(
        mode=PermissionMode.READ_ONLY, workspace=str(tmp_path), allow_approval=True
    )
    runtime = _shell_runtime(ledger, policy, approver)

    result = runtime.run(_spec("esc-broken"), ScriptedStrategy([_escalate("curl x")]))

    assert result.accepted is False
    assert approver.asked == [], "a broken policy must not reach the human"
    reasons = " ".join(str(item.get("reason")) for item in _decisions(ledger))
    assert "could not evaluate" in reasons


def test_a_policy_without_the_hook_is_skipped(tmp_path) -> None:
    """Custom policies predate #392; they simply have nothing to hard-refuse."""
    ledger = Ledger.in_memory("esc-plain")
    approver = _Yes(False)

    class Plain:
        def check(self, view, action):
            from xenon.kernel.contracts import PolicyDecision, PolicyDecisionKind

            return PolicyDecision(PolicyDecisionKind.ALLOW)

    runtime = _shell_runtime(ledger, Plain(), approver)

    result = runtime.run(_spec("esc-plain"), ScriptedStrategy([_escalate("curl x")]))

    assert approver.asked, "the human must still be asked"
    assert result.accepted is False


def test_an_approval_without_a_target_fails_closed(tmp_path) -> None:
    """No target means no grant: the human's yes must not become a blank cheque."""
    ledger = Ledger.in_memory("esc-notarget")
    approver = _Yes(True)
    runtime = _shell_runtime(ledger, _read_only(tmp_path), approver)

    result = runtime.run(
        _spec("esc-notarget"),
        ScriptedStrategy(
            [RequestEscalation(ToolIntent(capability=Capability.SHELL), "no params")]
        ),
    )

    assert result.accepted is False
    reasons = " ".join(str(item.get("reason")) for item in _decisions(ledger))
    assert "could not be scoped" in reasons


def test_a_granted_escalation_tells_the_model_the_exact_scope(tmp_path) -> None:
    """The granted call is bounded by its command; the model must see that."""
    from xenon.kernel.contracts import Abort

    ledger = Ledger.in_memory("esc-note")
    approver = _Yes(True)
    gateway = _Gateway()
    runtime = _shell_runtime(ledger, _read_only(tmp_path), approver, gateway)
    seen: list[tuple[str, ...]] = []

    class Spy:
        def __init__(self):
            self.step = 0

        def propose(self, view):
            seen.append(tuple(getattr(view, "notes", ()) or ()))
            self.step += 1
            if self.step == 1:
                return _escalate("curl -s wttr.in")
            if self.step == 2:
                return _shell("curl -s wttr.in")
            return Abort("done")

    runtime.run(_spec("esc-note"), Spy())

    assert any("Escalation approved for exactly this call" in " ".join(notes) for notes in seen)
    assert any("curl -s wttr.in" in " ".join(notes) for notes in seen)
