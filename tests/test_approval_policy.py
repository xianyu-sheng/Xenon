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
    SessionRuleStore,
    approval_rule_ttl_seconds,
    needs_boundary_approval,
    targets_outside_workspace,
    write_targets_for,
)
from xenon.nodes.tool_executor import ToolExecutor
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL


# ── 规则表 ─────────────────────────────────────────────────


def test_session_rule_allows_until_ttl_expires(monkeypatch):
    store = SessionRuleStore(ttl_seconds=10)
    store.allow("command")
    assert store.is_allowed("command") is True
    assert store.is_allowed("command", now=_now_plus(store, 5)) is True
    assert store.is_allowed("command", now=_now_plus(store, 11)) is False
    assert store.is_allowed("command") is False  # 过期即清除


def _now_plus(store: SessionRuleStore, seconds: float) -> float:
    import time

    return time.monotonic() + seconds


def test_rule_ttl_env_override(monkeypatch):
    monkeypatch.setenv("XENON_APPROVAL_TTL", "60")
    assert approval_rule_ttl_seconds() == 60.0
    monkeypatch.setenv("XENON_APPROVAL_TTL", "bogus")
    assert approval_rule_ttl_seconds() == 1800.0


# ── 路径与工具分类 ─────────────────────────────────────────


def test_write_targets_for_batch_and_single():
    assert write_targets_for("write_file", {"file_path": "a.txt"}) == ["a.txt"]
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


def test_always_records_a_ttl_rule_and_stops_prompting(monkeypatch, tmp_path):
    repl = _make_repl(tmp_path)
    _quiet_config(monkeypatch)
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("xenon.repl.repl.Prompt.ask", lambda *a, **k: "a")
    assert (
        repl._confirm_tool_approval(
            "write_file", {"file_path": str(tmp_path.parent / "b.txt")}
        )
        == APPROVAL_ALLOWED_ONCE
    )
    assert repl._approval_rules.is_allowed("write_file")
    monkeypatch.setattr(
        "xenon.repl.repl.Prompt.ask", lambda *a, **k: pytest.fail("规则已生效")
    )
    assert (
        repl._confirm_tool_approval(
            "write_file", {"file_path": str(tmp_path.parent / "c.txt")}
        )
        == APPROVAL_ALLOWED_ONCE
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
