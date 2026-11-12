"""Tool-boundary approval policy: workspace writes free, commands/outside ask.

User-approved decisions:
* workspace writes run without prompting;
* out-of-workspace writes and command execution ask;
* "a" records a session rule with a TTL (default 30 min);
* outcomes are closed and fail-closed: only ``allowed-once`` grants.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from xenon.engine.context import AgentContext
from xenon.nodes import tool_executor as te_mod
from xenon.nodes.approval_policy import (
    APPROVAL_ALLOWED_ONCE,
    APPROVAL_REJECTED,
    APPROVAL_UNAVAILABLE,
    approval_rule_ttl_seconds,
    needs_boundary_approval,
    targets_outside_workspace,
    write_targets_for,
)
from xenon.nodes.tool_executor import ToolExecutor
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL

# ── 规则表 ─────────────────────────────────────────────────


def test_rule_ttl_env_override(monkeypatch):
    monkeypatch.setenv("XENON_APPROVAL_TTL", "60")
    assert approval_rule_ttl_seconds() == 60.0
    monkeypatch.setenv("XENON_APPROVAL_TTL", "bogus")
    assert approval_rule_ttl_seconds() == 1800.0


# ── 路径与工具分类 ─────────────────────────────────────────


def test_write_targets_for_batch_and_single():
    assert write_targets_for("write_file", {"file_path": "a.txt"}) == ["a.txt"]
    assert write_targets_for(
        "apply_patch", {"files": [{"path": "a.txt", "content": "x"}]}
    ) == ["a.txt"]
    assert write_targets_for(
        "batch_write", {"files": [{"file_path": "a"}, {"file_path": "b"}]}
    ) == ["a", "b"]


def test_targets_outside_workspace(tmp_path):
    inside = {"file_path": str(tmp_path / "a.txt")}
    outside = {"file_path": str(tmp_path.parent / "b.txt")}
    assert targets_outside_workspace("write_file", inside, tmp_path) is False
    assert targets_outside_workspace("write_file", outside, tmp_path) is True
    # 写工具没有可解析目标 → 必须问
    assert targets_outside_workspace("write_file", {}, tmp_path) is True


def test_needs_boundary_approval_classification():
    assert needs_boundary_approval("read_file", {}) is False
    assert needs_boundary_approval("write_file", {}) is True
    assert needs_boundary_approval("command", {}) is True


# ── AgentContext 审批通道 ─────────────────────────────────


def test_context_allows_when_no_channel_registered():
    ctx = AgentContext()
    assert ctx.request_approval("command", {}) == APPROVAL_ALLOWED_ONCE


def test_context_returns_closed_outcome_and_fails_closed():
    ctx = AgentContext()
    ctx.set_approval_callback(lambda tool, params, reason: APPROVAL_REJECTED)
    assert ctx.request_approval("command", {}) == APPROVAL_REJECTED
    ctx.set_approval_callback(lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ctx.request_approval("command", {}) == APPROVAL_UNAVAILABLE
    ctx.set_approval_callback(lambda *a: "not-an-outcome")
    assert ctx.request_approval("command", {}) == APPROVAL_UNAVAILABLE


# ── REPL 回调 ─────────────────────────────────────────────


def _make_repl(tmp_path) -> REPL:
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl.project_ctx.root = tmp_path
    return repl


def _quiet_config(monkeypatch):
    monkeypatch.setattr(
        "xenon.repl.repl.get_config",
        lambda: SimpleNamespace(interaction=SimpleNamespace(assume_yes=False)),
    )


def test_workspace_write_is_allowed_without_prompt(monkeypatch, tmp_path):
    repl = _make_repl(tmp_path)
    _quiet_config(monkeypatch)
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(
        "xenon.repl.repl.Prompt.ask",
        lambda *a, **k: pytest.fail("工作区内写入不应弹询问"),
    )
    outcome = repl._confirm_tool_approval(
        "write_file", {"file_path": str(tmp_path / "a.txt")}
    )
    assert outcome == APPROVAL_ALLOWED_ONCE


def test_outside_write_prompts_and_honours_choice(monkeypatch, tmp_path):
    repl = _make_repl(tmp_path)
    _quiet_config(monkeypatch)
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("xenon.repl.repl.Prompt.ask", lambda *a, **k: "y")
    assert (
        repl._confirm_tool_approval(
            "write_file", {"file_path": str(tmp_path.parent / "b.txt")}
        )
        == APPROVAL_ALLOWED_ONCE
    )
    monkeypatch.setattr("xenon.repl.repl.Prompt.ask", lambda *a, **k: "n")
    assert (
        repl._confirm_tool_approval(
            "write_file", {"file_path": str(tmp_path.parent / "b.txt")}
        )
        == APPROVAL_REJECTED
    )


def test_always_records_a_timed_grant_for_that_path_only(monkeypatch, tmp_path):
    """§9: an ``a`` answer covers this tool + this path, for a limited time.

    It must not become "every write from now on": a different path still asks.
    """

    from xenon.kernel.contracts import Capability

    repl = _make_repl(tmp_path)
    _quiet_config(monkeypatch)
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("xenon.repl.repl.Prompt.ask", lambda *a, **k: "a")
    target = str(tmp_path.parent / "b.txt")
    assert (
        repl._confirm_tool_approval("write_file", {"file_path": target})
        == APPROVAL_ALLOWED_ONCE
    )

    book = repl._grant_book
    assert len(book) == 1
    assert book.active(str(tmp_path), Capability.EDIT, [target])

    # A different path is not covered by that answer.
    monkeypatch.setattr(
        "xenon.repl.repl.Prompt.ask", lambda *a, **k: pytest.fail("该路径未被授权")
    )
    with pytest.raises(pytest.fail.Exception):
        repl._confirm_tool_approval(
            "write_file", {"file_path": str(tmp_path.parent / "c.txt")}
        )


def test_command_prompts_even_inside_workspace(monkeypatch, tmp_path):
    repl = _make_repl(tmp_path)
    _quiet_config(monkeypatch)
    monkeypatch.setattr(sys, "stdin", _Tty())
    asked: list[str] = []
    monkeypatch.setattr(
        "xenon.repl.repl.Prompt.ask", lambda *a, **k: asked.append("yes") or "y"
    )
    assert repl._confirm_tool_approval("command", {"command": "echo hi"}) == (
        APPROVAL_ALLOWED_ONCE
    )
    assert asked == ["yes"]


def test_non_interactive_is_unavailable(monkeypatch, tmp_path):
    repl = _make_repl(tmp_path)
    _quiet_config(monkeypatch)
    monkeypatch.setattr(sys, "stdin", _NoTty())
    assert (
        repl._confirm_tool_approval("command", {"command": "echo hi"})
        == APPROVAL_UNAVAILABLE
    )


class _Tty:
    def isatty(self) -> bool:
        return True


class _NoTty:
    def isatty(self) -> bool:
        return False


# ── 执行器接线 ─────────────────────────────────────────────


class _FakeNode:
    script: list = []

    def __init__(self, name, action_type=None, **params):
        self.action_type = action_type

    @staticmethod
    def normalize_params(p):
        return p

    def execute(self, context):
        if not _FakeNode.script:
            return {"success": True, "content": "default"}
        return _FakeNode.script.pop(0)


def test_executor_denies_when_boundary_rejects(monkeypatch):
    monkeypatch.setattr(te_mod, "ToolNode", _FakeNode)
    _FakeNode.script = [{"success": True, "content": "ran"}]
    ctx = AgentContext({"_execution_level": 3})
    seen: list[str] = []

    def callback(tool, params, reason):
        seen.append(tool)
        return APPROVAL_REJECTED

    ctx.set_approval_callback(callback)
    result = ToolExecutor(retry_attempts=1).execute(
        "command", {"command": "echo hi"}, ctx, tools={"command": {}}
    )

    assert result.success is False
    assert "未授权" in result.observation
    assert seen == ["command"]
    assert _FakeNode.script  # 工具体没有执行


def test_executor_runs_when_boundary_allows(monkeypatch):
    monkeypatch.setattr(te_mod, "ToolNode", _FakeNode)
    _FakeNode.script = [{"success": True, "content": "ran"}]
    ctx = AgentContext({"_execution_level": 3})
    ctx.set_approval_callback(lambda *a: APPROVAL_ALLOWED_ONCE)
    result = ToolExecutor(retry_attempts=1).execute(
        "command", {"command": "echo hi"}, ctx, tools={"command": {}}
    )

    assert result.success is True
    assert not _FakeNode.script


def test_a_remembered_answer_stops_the_panel_reappearing(tmp_path):
    """§9: after ``a``, the same tool+path is decided without a prompt.

    The assertion is the *policy decision*, because that is what decides
    whether the runtime asks: a REQUIRE_APPROVAL reaches the panel, ALLOW does
    not.  (The previous version seeded the book and then asserted the book —
    it stayed green even if the policy stopped consulting grants.)
    """

    from xenon.kernel.contracts import (
        CallTool,
        Capability,
        PolicyDecisionKind,
        ToolIntent,
    )
    from xenon.kernel.policies import PermissionMode, PermissionPolicy
    from xenon.kernel.session_grants import ALLOW, grant_from_choice

    grant = grant_from_choice(
        ALLOW,
        capability=Capability.SHELL,
        targets=("echo hi",),
        workspace=str(tmp_path),
    )
    assert grant is not None
    policy = PermissionPolicy(
        mode=PermissionMode.SCOPED,
        workspace=str(tmp_path),
        allow_approval=True,
        grants=grant.as_scoped(),
    )

    decision = policy.check(
        None,  # the policy is intentionally view-free for a plain intent
        CallTool(
            ToolIntent(capability=Capability.SHELL, params={"command": "echo hi"})
        ),
    )

    assert decision.kind is PolicyDecisionKind.ALLOW


def test_boundary_approval_records_allowed_once_and_uses_typed_prompt(
    monkeypatch, tmp_path
):
    repl = _make_repl(tmp_path)
    _quiet_config(monkeypatch)
    monkeypatch.setattr(sys, "stdin", _Tty())
    seen: dict = {}
    monkeypatch.setattr(
        "xenon.repl.repl.Prompt.ask",
        lambda *a, **k: seen.update(kwargs=k, prompt=a[0]) or "y",
    )

    outcome = repl._confirm_tool_approval("command", {"command": "echo hi"})

    assert outcome == APPROVAL_ALLOWED_ONCE
    # "本次允许" is not remembered — the call in flight already has it.
    assert len(repl._grant_book) == 0
    assert seen["prompt"] == "选择"


def test_command_with_workspace_file_path_still_prompts(monkeypatch, tmp_path):
    """H-2 回归：命令带工作区内 file_path 不得绕过审批面板（执行级必问）。"""

    repl = _make_repl(tmp_path)
    _quiet_config(monkeypatch)
    monkeypatch.setattr(sys, "stdin", _Tty())
    asked = []
    monkeypatch.setattr(
        "xenon.repl.repl.Prompt.ask",
        lambda *a, **k: asked.append(1) or "y",
    )
    outcome = repl._confirm_tool_approval(
        "command",
        {"command": "git status", "file_path": str(tmp_path / "a.txt")},
    )
    assert asked, "命令必须走审批面板，带工作区内 file_path 也不例外"
    assert outcome == APPROVAL_ALLOWED_ONCE


def test_allowed_once_scoped_to_target_paths(monkeypatch, tmp_path):
    """§9: ``y`` = 本次允许, so it must not be remembered at all.

    This used to assert "same path is not asked again within the round", which
    is the ``allowed-once`` memory the old boundary store provided.  §9 has no
    such answer — ``y`` is "只放行这一次，下次再用仍需询问" — so the second call
    to the same path asks again, and only ``a`` (tool + path, timed) stops it.
    """

    repl = _make_repl(tmp_path)
    _quiet_config(monkeypatch)
    monkeypatch.setattr(sys, "stdin", _Tty())
    asked = []
    monkeypatch.setattr(
        "xenon.repl.repl.Prompt.ask", lambda *a, **k: asked.append(1) or "y"
    )
    outside = tmp_path.parent
    p1, p2 = str(outside / "x1.txt"), str(outside / "x2.txt")

    assert repl._confirm_tool("write_file", {"file_path": p1}, "HIGH")[0] is True
    assert len(asked) == 1
    # Same path again: "本次" was for the previous call only.
    assert repl._confirm_tool("write_file", {"file_path": p1}, "HIGH")[0] is True
    assert len(asked) == 2
    # A different path asks as well.
    assert repl._confirm_tool("write_file", {"file_path": p2}, "HIGH")[0] is True
    assert len(asked) == 3
    # And nothing was remembered by any of those "y" answers.
    assert len(repl._grant_book) == 0


# ── 跨层契约：内核已裁决的调用不再由执行器二次裁决 ───────────────
#
# 同一动作曾有两处决策，且方向相反：内核 allow 的工作区内写被 Stage 1.6
# 拉回来追问，内核 veto 的越界写却能靠面板放行。执行器跑在内核之下，它的
# 兜底是为「根本没有内核」的库调用方准备的——那些调用方不会设这个标记。


def test_kernel_adjudicated_call_is_not_asked_twice():
    ctx = AgentContext()
    asked = []
    ctx.set_approval_callback(
        lambda tool, params, reason: asked.append(tool) or APPROVAL_REJECTED
    )
    ctx.set("_kernel_adjudicated", True)

    assert ctx.request_approval("apply_patch", {}) == APPROVAL_ALLOWED_ONCE
    assert asked == [], "内核已裁决的调用不得再次询问"


def test_without_the_flag_the_boundary_channel_still_asks():
    """库/直连调用方没有内核，兜底必须仍然生效。"""

    ctx = AgentContext()
    asked = []
    ctx.set_approval_callback(
        lambda tool, params, reason: asked.append(tool) or APPROVAL_REJECTED
    )

    assert ctx.request_approval("apply_patch", {}) == APPROVAL_REJECTED
    assert asked == ["apply_patch"]


def test_the_flag_does_not_survive_a_denied_kernel_ruling(tmp_path):
    """标记必须在执行后清除，否则下一次调用会被静默放行。"""

    from xenon.catalog.gateway import LegacyToolGateway

    ctx = AgentContext()
    gateway = LegacyToolGateway(
        specs=(),
        workspace=tmp_path,
        context=ctx,
        policy=object(),  # 只为验证标记；下面直接调用辅助方法
    )

    gateway._mark_kernel_ruling(True)
    assert ctx.get("_kernel_adjudicated") is True
    gateway._mark_kernel_ruling(False)
    assert ctx.get("_kernel_adjudicated") is False


def test_needs_boundary_approval_ignores_workspace_and_mode():
    """Stage 1.6 的判定只看执行级别——这正是它必须让位给内核的原因。

    它不接收 workspace，也不接收权限模式，所以无法区分「工作区内写」
    与「越界写」。把它当作权威，就等于用一条信息更少的规则覆盖内核裁决。
    """

    inside = str(__import__("pathlib").Path.cwd() / "note.txt")
    outside = str(__import__("tempfile").gettempdir())
    for path in (inside, outside):
        assert needs_boundary_approval("apply_patch", {"files": [{"path": path}]})


def test_an_adjudicated_write_is_never_auto_allowed(monkeypatch, tmp_path):
    """`scoped` writes must reach the panel even inside the workspace.

    The kernel asks only when the mode says a human must answer; the UI used
    to answer for them, which made `/permissions default` behave exactly like
    `accept_edits`.
    """

    repl = _make_repl(tmp_path)
    _quiet_config(monkeypatch)
    monkeypatch.setattr(sys, "stdin", _Tty())
    asked: list[str] = []
    monkeypatch.setattr(
        "xenon.repl.repl.Prompt.ask",
        lambda *a, **k: asked.append(str(a[0])) or "y",
    )

    outcome = repl._confirm_tool_approval(
        "write_file",
        {"file_path": str(tmp_path / "a.py"), "content": "x"},
        adjudicated=True,
    )

    assert asked, "a scoped write must be put to the human"
    assert outcome == APPROVAL_ALLOWED_ONCE
