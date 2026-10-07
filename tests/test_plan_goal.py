"""Plan mode (logged soft guidance) + goal projection + /plan //goal commands."""

from __future__ import annotations

from xenon.repl.command_groups.goal import _cmd_goal
from xenon.repl.command_groups.plan import _cmd_plan
from xenon.repl.execution_policy import ExecutionLevel
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL
from xenon.repl.turn_contract import build_turn_contract, contract_for_level
from xenon.session.projection import project


def _make_repl(tmp_path, monkeypatch) -> REPL:
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    return REPL(registry=registry, streaming=False)


# ── 计划模式 ───────────────────────────────────────────────


def test_plan_mode_contract_defers_write_without_losing_proposal():
    contract = build_turn_contract("把结果写到 output.txt")
    assert contract.level >= ExecutionLevel.WRITE

    deferred = REPL._plan_mode_contract(contract)

    assert deferred.level < ExecutionLevel.WRITE
    assert deferred.proposed_level >= ExecutionLevel.WRITE
    assert deferred.deferred_write is True
    assert "计划模式" in deferred.reason


def test_plan_command_toggles_and_logs_state(tmp_path, monkeypatch):
    repl = _make_repl(tmp_path, monkeypatch)
    assert repl._plan_mode_active is False

    assert "开启" in _cmd_plan(args="on", session_state={"_repl": repl})
    assert repl._plan_mode_active is True
    assert "开启" in _cmd_plan(args="status", session_state={"_repl": repl})

    events = repl._session_events.read()
    assert any(
        e["type"] == "plan_mode/set" and e["active"] is True for e in events
    )

    assert "关闭" in _cmd_plan(args="off", session_state={"_repl": repl})
    assert repl._plan_mode_active is False


def test_plan_mode_state_loads_from_events(tmp_path, monkeypatch):
    repl = _make_repl(tmp_path, monkeypatch)
    log = repl._session_events
    log.append("plan_mode/set", active=True)
    log.append("plan_mode/set", active=False)
    log.append("plan_mode/set", active=True)
    repl._session_events = log
    assert repl._load_plan_mode() is True


def test_handle_chat_in_plan_mode_opens_a_gate(tmp_path, monkeypatch):
    repl = _make_repl(tmp_path, monkeypatch)
    repl._plan_mode_active = True
    monkeypatch.setattr(repl, "_run_direct", lambda *a, **k: None)
    monkeypatch.setattr(repl, "_run_engine", lambda *a, **k: None)
    repl.ctx_mgr.add_assistant_message("方案如下……")

    repl._handle_chat("把结果写到 output.txt")

    contract = repl.agent_context.get("_turn_contract")
    assert contract.level < ExecutionLevel.WRITE
    assert contract.deferred_write is True
    assert repl._pending_action is not None
    assert repl._pending_action.kind == "approval"


# ── 目标投影 ───────────────────────────────────────────────


def test_goal_opens_pauses_and_survives_continuation(tmp_path, monkeypatch):
    repl = _make_repl(tmp_path, monkeypatch)
    first = contract_for_level(ExecutionLevel.READ_ONLY)

    repl._update_goal_projection("帮我做一下快排算法脚本", first, continuation=False)
    repl._update_goal_projection("分析一下 SmartBench 项目架构", first, continuation=False)
    repl._update_goal_projection("继续", first, continuation=True)

    view = project(repl._session_events.read())
    assert [g.status for g in view.goals] == ["paused", "active"]
    assert view.active_goal is not None
    assert "SmartBench" in view.active_goal.objective
    types = [e["type"] for e in repl._session_events.read()]
    assert "goal/paused" in types


def test_goal_command_reports_projection(tmp_path, monkeypatch):
    repl = _make_repl(tmp_path, monkeypatch)
    repl._update_goal_projection(
        "重构文档读取模块", contract_for_level(ExecutionLevel.READ_ONLY), continuation=False
    )

    output = _cmd_goal(session_state={"_repl": repl})

    assert "重构文档读取模块" in output
