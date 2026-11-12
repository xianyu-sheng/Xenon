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

    def _confirm_tool(self, tool: str, params: dict, risk: str, *, from_child=False):
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
        def _confirm_tool(self, tool, params, risk, *, from_child=False):
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
    from xenon.kernel.runtime import _POLICY_REFUSED, LoopRuntime
    from xenon.ledger.events import EventKind
    from xenon.ledger.store import Ledger

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

    # A denial refuses *this action* and tells the loop to continue; it is not
    # a blocked result any more (#423).
    assert result is _POLICY_REFUSED
    decisions = ledger.read(EventKind.CONTROL_DECISION)
    assert decisions, "the refusal must be in the ledger, not only in memory"
    assert "用户拒绝" in str(decisions[-1].payload.get("reason"))
    assert decisions[-1].payload.get("continued") is True


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
    from xenon.replay import blocking_reason

    ledger = Ledger(tmp_path / "t.jsonl", "t")
    ledger.append(
        EventKind.POLICY_DECISION,
        EventSource.GATE,
        {"kind": "veto", "reason": "shell requires a grant", "scope": ["shell", "ls"]},
    )

    reason = blocking_reason(ledger.path, task_id="t")

    assert "shell requires a grant" in reason
    assert "ls" in reason


def test_blocking_reason_skips_an_approved_approval(tmp_path):
    from xenon.ledger.events import EventKind, EventSource
    from xenon.ledger.store import Ledger
    from xenon.replay import blocking_reason

    ledger = Ledger(tmp_path / "t.jsonl", "t")
    ledger.append(
        EventKind.USER_APPROVAL,
        EventSource.USER,
        {"approved": True, "reason": "fine"},
    )

    assert blocking_reason(ledger.path, task_id="t") == ""


def test_blocking_reason_names_a_rejected_approval(tmp_path):
    from xenon.ledger.events import EventKind, EventSource
    from xenon.ledger.store import Ledger
    from xenon.replay import blocking_reason

    ledger = Ledger(tmp_path / "t.jsonl", "t")
    ledger.append(
        EventKind.USER_APPROVAL,
        EventSource.USER,
        {"approved": False, "reason": "用户取消任务"},
    )

    assert "审批未通过" in blocking_reason(ledger.path, task_id="t")
    assert "用户取消任务" in blocking_reason(ledger.path, task_id="t")


def test_blocking_reason_is_empty_when_the_ledger_is_unreadable(tmp_path):
    from xenon.replay import blocking_reason

    assert blocking_reason(tmp_path / "missing.jsonl", task_id="t") == ""


def test_blocking_reason_is_empty_for_an_unexplained_run(tmp_path):
    from xenon.ledger.events import EventKind, EventSource
    from xenon.ledger.store import Ledger
    from xenon.replay import blocking_reason

    ledger = Ledger(tmp_path / "t.jsonl", "t")
    ledger.append(EventKind.TASK_STARTED, EventSource.RUNTIME, {"objective": "o"})

    assert blocking_reason(ledger.path, task_id="t") == ""


# ── 审批读答：统一走 rich Prompt，协议宿主与测试都能替换它 ─────


class _Ux:
    """Just enough of ApprovalUxMixin to exercise the reader."""


def test_the_reader_goes_through_the_patchable_prompt(monkeypatch):
    import xenon.repl.approval_ux as mod
    from xenon.repl.approval_ux import ApprovalUxMixin

    seen: dict = {}

    def ask(*args, **kwargs):
        seen.update(kwargs)
        return "a"

    monkeypatch.setattr(mod.Prompt, "ask", ask)

    assert ApprovalUxMixin._ask_approval_choice(_Ux()) == "a"
    assert seen["default"] == "n"
    assert seen["choices"] == ["y", "t", "a", "n", "q"]


def test_blocking_reason_surfaces_an_unanswered_user_question(tmp_path):
    """#402: a run that asked the user something is *waiting*, not failed.

    The payload carries ``question`` rather than ``reason``; the old kind
    whitelist ignored the event entirely, so a blocked turn rendered as
    "任务未交付（phase=blocked）。" with the question nowhere on screen.
    """
    from xenon.kernel.contracts import TaskPhase, TaskResult
    from xenon.ledger.events import EventKind, EventSource
    from xenon.ledger.store import Ledger
    from xenon.repl.runtime_adapter import _result_summary
    from xenon.replay import blocking_reason

    ledger = Ledger(tmp_path / "q.jsonl", "q")
    ledger.append(
        EventKind.USER_QUESTION,
        EventSource.LLM,
        {"question": "请提供 P0 对应的 issue 或文件"},
    )
    ledger.append(
        EventKind.STATE_TRANSITION,
        EventSource.RUNTIME,
        {"from": "running", "to": "blocked", "reason": "user input required"},
    )

    reason = blocking_reason(ledger.path, task_id="q")

    assert "等待你的回答" in reason
    assert "请提供 P0 对应的 issue 或文件" in reason

    summary = _result_summary(
        TaskResult(
            task_id="q",
            phase=TaskPhase.BLOCKED,
            answer="",
            accepted=False,
        ),
        ledger_path=ledger.path,
    )
    assert "请提供 P0 对应的 issue" in summary


def test_a_waiting_turn_is_titled_waiting_not_blocked(tmp_path):
    """#402: "Kernel · blocked" for a turn that simply asked a question."""
    from xenon.kernel.contracts import TaskPhase, TaskResult
    from xenon.ledger.events import EventKind, EventSource
    from xenon.ledger.store import Ledger
    from xenon.repl.runtime_adapter import _result_summary
    from xenon.replay import blocking_reason

    ledger = Ledger(tmp_path / "w.jsonl", "w")
    ledger.append(EventKind.USER_QUESTION, EventSource.LLM, {"question": "哪个仓库？"})

    summary = _result_summary(
        TaskResult(task_id="w", accepted=False, phase=TaskPhase.BLOCKED, answer=""),
        ledger_path=ledger.path,
    )

    assert summary == "等待你的回答：哪个仓库？"
    assert "未交付" not in summary
    assert blocking_reason(ledger.path, task_id="w") == "等待你的回答：哪个仓库？"


def test_an_empty_question_does_not_hide_a_real_reason(tmp_path):
    """A user_question with no text must not shadow an earlier explanation."""
    from xenon.ledger.events import EventKind, EventSource
    from xenon.ledger.store import Ledger
    from xenon.replay import blocking_reason

    ledger = Ledger(tmp_path / "e.jsonl", "e")
    ledger.append(
        EventKind.POLICY_DECISION,
        EventSource.GATE,
        {"kind": "veto", "reason": "shell requires a grant"},
    )
    ledger.append(EventKind.USER_QUESTION, EventSource.LLM, {"question": "  "})

    assert blocking_reason(ledger.path, task_id="e") == "shell requires a grant"
