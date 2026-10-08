"""D1：submit_plan 计划审批（面板批准/驳回，批准退出计划模式）。"""

from __future__ import annotations

import sys


from xenon.engine.context import AgentContext
from xenon.nodes import tool_executor as te_mod
from xenon.nodes.tool_executor import ToolExecutor
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL


def _make_repl(tmp_path, monkeypatch) -> REPL:
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    return REPL(registry=registry, streaming=False)


def test_confirm_plan_approve_exits_plan_mode_and_records(monkeypatch, tmp_path):
    repl = _make_repl(tmp_path, monkeypatch)
    repl._plan_mode_active = True
    monkeypatch.setattr(sys, "stdin", type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("xenon.repl.repl.Prompt.ask", lambda *a, **k: "y")

    result = repl._confirm_plan("步骤1：写文件")

    assert result == {"approved": True, "feedback": ""}
    assert repl._plan_mode_active is False
    events = repl._session_events.read()
    assert any(e["type"] == "plan/approved" for e in events)


def test_confirm_plan_reject_keeps_plan_mode(monkeypatch, tmp_path):
    repl = _make_repl(tmp_path, monkeypatch)
    repl._plan_mode_active = True
    monkeypatch.setattr(sys, "stdin", type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("xenon.repl.repl.Prompt.ask", lambda *a, **k: "n")

    result = repl._confirm_plan("步骤1")

    assert result["approved"] is False
    assert "修改计划" in result["feedback"]
    assert repl._plan_mode_active is True


class _FakeNode:
    def __init__(self, name, action_type=None, **params):
        pass

    @staticmethod
    def normalize_params(p):
        return p

    def execute(self, context):
        return {"success": True}


def test_executor_submit_plan_approved_and_rejected(monkeypatch):
    monkeypatch.setattr(te_mod, "ToolNode", _FakeNode)

    ctx = AgentContext()
    ctx.set_plan_callback(lambda plan: {"approved": True, "feedback": ""})
    result = ToolExecutor(retry_attempts=1).execute(
        "submit_plan", {"plan": "步骤1"}, ctx, tools={"submit_plan": {}}
    )
    assert result.success is True

    ctx.set_plan_callback(lambda plan: {"approved": False, "feedback": "重做"})
    result = ToolExecutor(retry_attempts=1).execute(
        "submit_plan", {"plan": "步骤1"}, ctx, tools={"submit_plan": {}}
    )
    assert result.success is False
    assert "重做" in result.observation


def test_executor_submit_plan_fails_closed_without_channel(monkeypatch):
    monkeypatch.setattr(te_mod, "ToolNode", _FakeNode)
    ctx = AgentContext()
    result = ToolExecutor(retry_attempts=1).execute(
        "submit_plan", {"plan": "步骤1"}, ctx, tools={"submit_plan": {}}
    )
    assert result.success is False
    assert "无交互通道" in result.observation
