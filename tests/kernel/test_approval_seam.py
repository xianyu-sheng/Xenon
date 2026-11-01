"""Approval seam: a granted approval must actually let the tool run.

The bug this file guards against: the REPL asked, the operator answered "y",
the run still ended blocked — because the bridge returned a bare bool, the
runtime lost the reason, and a failed grant looked identical to a refusal.
"""

from __future__ import annotations

from types import SimpleNamespace

import xenon.kernel  # noqa: F401  — breaks the documented circular import
from xenon.catalog.catalog import build_catalog
from xenon.kernel.contracts import (
    Capability,
    PolicyDecision,
    PolicyDecisionKind,
    TaskMode,
    TaskPhase,
    TaskSpec,
    ToolIntent,
)
from xenon.kernel.policies import PermissionMode, PermissionPolicy
from xenon.kernel.view import build_model_view
from xenon.ledger.store import Ledger
from xenon.repl.runtime_adapter import KernelApprovalProvider


def _view():
    spec = TaskSpec(task_id="t", objective="o", workspace=".", mode=TaskMode.TASK)
    return build_model_view(
        spec=spec, catalog=build_catalog("core"), ledger=Ledger.in_memory("t")
    )


class _Repl:
    def __init__(self, answer: bool, reason: str = "") -> None:
        self.answer = answer
        self.reason = reason
        self.seen: list[tuple[str, dict, str]] = []

    def _confirm_tool(self, tool: str, params: dict, risk: str):
        self.seen.append((tool, params, risk))
        return (self.answer, self.reason)


def _action(capability: Capability, params: dict):
    from xenon.kernel.contracts import CallTool

    return CallTool(ToolIntent(capability=capability, params=params))


def _policy(**kwargs) -> PermissionPolicy:
    return PermissionPolicy(mode=PermissionMode.AUTO_EDIT, workspace=".", **kwargs)


def test_an_approved_remote_call_yields_a_grant():
    policy = _policy(allow_approval=True)
    action = _action(Capability.REMOTE, {"tool_name": "search_repositories"})
    view = _view()

    decision = policy.check(view, action)
    assert decision.kind is PolicyDecisionKind.REQUIRE_APPROVAL

    grants = tuple(policy.approve(decision, action))

    assert [grant.target for grant in grants] == ["search_repositories"]
    # With the grant in place the same call is allowed.
    assert policy.check(view, action).kind is PolicyDecisionKind.ALLOW


def test_the_bridge_reports_the_front_ends_verdict():
    provider = KernelApprovalProvider(_Repl(answer=True))

    assert provider.request(_view(), _action(Capability.REMOTE, {"tool_name": "x"}),
                            PolicyDecision(PolicyDecisionKind.REQUIRE_APPROVAL)) is True
    assert provider.last_reason == ""


def test_a_refusal_keeps_the_reason_for_the_ledger():
    provider = KernelApprovalProvider(_Repl(answer=False, reason="用户拒绝"))

    provider.request(_view(), _action(Capability.REMOTE, {"tool_name": "x"}),
                     PolicyDecision(PolicyDecisionKind.REQUIRE_APPROVAL))

    assert provider.last_reason == "用户拒绝"


def test_a_refusal_without_a_reason_says_so():
    provider = KernelApprovalProvider(_Repl(answer=False))

    provider.request(_view(), _action(Capability.SHELL, {"command": "ls"}),
                     PolicyDecision(PolicyDecisionKind.REQUIRE_APPROVAL))

    assert "rejected" in provider.last_reason


def test_a_front_end_without_a_confirmation_ui_denies_with_a_reason():
    provider = KernelApprovalProvider(SimpleNamespace())

    approved = provider.request(
        _view(),
        _action(Capability.SHELL, {"command": "ls"}),
        PolicyDecision(PolicyDecisionKind.REQUIRE_APPROVAL),
    )

    assert approved is False
    assert "confirmation" in provider.last_reason


def test_a_broken_confirmation_ui_denies_with_a_reason():
    class _Boom:
        def _confirm_tool(self, tool, params, risk):
            raise RuntimeError("tty died")

    provider = KernelApprovalProvider(_Boom())

    approved = provider.request(
        _view(),
        _action(Capability.SHELL, {"command": "ls"}),
        PolicyDecision(PolicyDecisionKind.REQUIRE_APPROVAL),
    )

    assert approved is False
    assert "tty died" in provider.last_reason


def test_the_kernel_records_the_providers_reason():
    """The blocked reason must name the real cause, not the policy's label."""

    from xenon.catalog.gateway import LegacyToolGateway
    from xenon.kernel.controller import VerificationController
    from xenon.kernel.runtime import LoopRuntime

    provider = KernelApprovalProvider(_Repl(answer=False, reason="用户拒绝"))
    runtime = LoopRuntime(
        catalog=build_catalog("core"),
        gateway=LegacyToolGateway(),
        controller=VerificationController(),
        ledger=Ledger.in_memory("t"),
        approval=provider,
        policies=(_policy(allow_approval=True),),
    )

    approved, reason = runtime._request_approval(
        _view(),
        _action(Capability.SHELL, {"command": "ls"}),
        PolicyDecision(PolicyDecisionKind.REQUIRE_APPROVAL, reason="shell requires approval"),
    )

    assert approved is False
    assert reason == "用户拒绝"


def test_a_blocked_run_explains_itself_from_the_ledger(tmp_path):
    """The operator must not have to read the ledger to learn why it stopped."""

    from xenon.catalog.gateway import LegacyToolGateway
    from xenon.kernel.controller import VerificationController
    from xenon.kernel.runtime import LoopRuntime
    from xenon.ledger.store import Ledger
    from xenon.repl.runtime_adapter import _result_summary

    ledger = Ledger(tmp_path / "t.jsonl", "t")
    runtime = LoopRuntime(
        catalog=build_catalog("core"),
        gateway=LegacyToolGateway(),
        controller=VerificationController(),
        ledger=ledger,
        approval=KernelApprovalProvider(_Repl(answer=False, reason="用户拒绝")),
        policies=(_policy(allow_approval=True),),
    )
    runtime._spec = TaskSpec(task_id="t", objective="o", workspace=".")
    # The runtime is already running; the transition to BLOCKED is legal from
    # here but not from CREATED.
    runtime._phase = TaskPhase.RUNNING
    action = _action(Capability.SHELL, {"command": "ls"})
    result = runtime._check_policies(_view(), action)

    assert result is not None, "a denied approval must stop the action"
    summary = _result_summary(result, ledger_path=ledger.path)

    assert "blocked" in summary
    assert "用户拒绝" in summary


# ── blocked 的自解释渲染：各分支都要有确定性行为 ─────────────────────


def _write_ledger(path, events):
    from xenon.ledger.store import Ledger

    ledger = Ledger(path, "t")
    for kind, payload in events:
        ledger.append(kind, __import__("xenon.ledger.events", fromlist=["EventSource"]).EventSource.GATE, payload)
    return path


def test_blocking_reason_reads_a_policy_decision_with_scope(tmp_path):
    from xenon.ledger.events import EventKind, EventSource
    from xenon.ledger.store import Ledger
    from xenon.repl.runtime_adapter import _blocking_reason

    ledger = Ledger(tmp_path / "t.jsonl", "t")
    ledger.append(
        EventKind.POLICY_DECISION,
        EventSource.GATE,
        {"kind": "veto", "reason": "shell requires a grant", "scope": ["shell", "ls"]},
    )

    reason = _blocking_reason(ledger.path, task_id="t")

    assert "shell requires a grant" in reason
    assert "ls" in reason


def test_blocking_reason_skips_an_approved_approval(tmp_path):
    from xenon.ledger.events import EventKind, EventSource
    from xenon.ledger.store import Ledger
    from xenon.repl.runtime_adapter import _blocking_reason

    ledger = Ledger(tmp_path / "t.jsonl", "t")
    ledger.append(
        EventKind.USER_APPROVAL,
        EventSource.USER,
        {"approved": True, "reason": "fine"},
    )

    assert _blocking_reason(ledger.path, task_id="t") == ""


def test_blocking_reason_names_a_rejected_approval(tmp_path):
    from xenon.ledger.events import EventKind, EventSource
    from xenon.ledger.store import Ledger
    from xenon.repl.runtime_adapter import _blocking_reason

    ledger = Ledger(tmp_path / "t.jsonl", "t")
    ledger.append(
        EventKind.USER_APPROVAL,
        EventSource.USER,
        {"approved": False, "reason": "用户取消任务"},
    )

    assert "审批未通过" in _blocking_reason(ledger.path, task_id="t")
    assert "用户取消任务" in _blocking_reason(ledger.path, task_id="t")


def test_blocking_reason_is_empty_when_the_ledger_is_unreadable(tmp_path):
    from xenon.repl.runtime_adapter import _blocking_reason

    assert _blocking_reason(tmp_path / "missing.jsonl", task_id="t") == ""


def test_blocking_reason_is_empty_for_an_unexplained_run(tmp_path):
    from xenon.ledger.events import EventKind, EventSource
    from xenon.ledger.store import Ledger
    from xenon.repl.runtime_adapter import _blocking_reason

    ledger = Ledger(tmp_path / "t.jsonl", "t")
    ledger.append(EventKind.TASK_STARTED, EventSource.RUNTIME, {"objective": "o"})

    assert _blocking_reason(ledger.path, task_id="t") == ""


# ── 输入所有权：pt 会话存在时必须走它，否则会和 input() 抢 stdin ──


class _Session:
    def __init__(self, answers):
        self.answers = list(answers)
        self.asked: list[str] = []

    def prompt(self, message, default="n"):
        self.asked.append(message)
        return self.answers.pop(0)


class _Ux:
    """Just enough of ApprovalUxMixin to exercise the reader choice."""

    def __init__(self, session):
        self._pt_session = session
        self.printed: list[str] = []


def test_the_reader_prefers_prompt_toolkit_when_a_session_exists():
    from xenon.repl.approval_ux import ApprovalUxMixin

    session = _Session(["A"])
    ux = _Ux(session)
    ux._active_runtime = object()

    answer = ApprovalUxMixin._ask_approval_choice(ux)

    assert answer == "a", "the answer is normalised to lower case"
    assert session.asked, "prompt_toolkit must own the read"


def test_an_empty_answer_takes_the_default():
    from xenon.repl.approval_ux import ApprovalUxMixin

    ux = _Ux(_Session([""]))
    ux._active_runtime = object()
    answer = ApprovalUxMixin._ask_approval_choice(ux)

    assert answer == "n"


def test_an_unrecognised_answer_is_asked_again():
    from xenon.repl.approval_ux import ApprovalUxMixin

    session = _Session(["maybe", "y"])
    ux = _Ux(session)
    ux._active_runtime = object()

    assert ApprovalUxMixin._ask_approval_choice(ux) == "y"
    assert len(session.asked) == 2


def test_the_reader_uses_prompt_toolkit_only_inside_a_turn():
    """Outside a kernel turn the historic input() path must stay in charge."""

    from xenon.repl.approval_ux import ApprovalUxMixin

    session = _Session(["y"])
    ux = _Ux(session)
    ux._active_runtime = None

    # No active runtime -> the session must not be asked; the fallback is
    # rich's Prompt, which the caller patches in tests.
    called: list[str] = []

    class _Prompt:
        @staticmethod
        def ask(*args, **kwargs):
            called.append("asked")
            return "y"

    import xenon.repl.approval_ux as mod

    original = mod.Prompt
    mod.Prompt = _Prompt
    try:
        assert ApprovalUxMixin._ask_approval_choice(ux) == "y"
    finally:
        mod.Prompt = original
    assert called == ["asked"]
    assert session.asked == []


def test_the_reader_uses_the_session_inside_a_turn():
    from xenon.repl.approval_ux import ApprovalUxMixin

    session = _Session(["a"])
    ux = _Ux(session)
    ux._active_runtime = object()

    assert ApprovalUxMixin._ask_approval_choice(ux) == "a"
    assert session.asked, "inside a turn the session owns the terminal"
