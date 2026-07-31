"""Permission-gate integration tests for the real REPL execution path."""

from __future__ import annotations

import importlib
import io

import pytest
from rich.console import Console
from rich.panel import Panel

from xenon.engine.context import AgentContext
from xenon.engine.callbacks import SilentCallback
from xenon.engine.react_engine import BUILTIN_TOOLS, ReActEngine
from xenon.nodes.tool_executor import ToolExecutor, classify_tool
from xenon.repl.permissions import (
    PermissionDecision,
    PermissionGate,
    PermissionMode,
    PermissionState,
)


def test_default_gate_fails_closed_without_confirmation_callback():
    gate = PermissionGate(PermissionMode.DEFAULT)

    allowed, reason = gate.check("write_file", {"file_path": "result.txt"})

    assert allowed is False
    assert "需要确认" in reason
    assert gate.state is PermissionState.FAILED
    assert gate.last_request is not None
    assert gate.last_request.tool_name == "write_file"


def test_permission_state_machine_tracks_approval_denial_and_cancel():
    gate = PermissionGate(PermissionMode.DEFAULT)
    responses = iter([
        PermissionDecision.ALLOW_ONCE,
        PermissionDecision.DENY,
        PermissionDecision.CANCEL,
    ])
    gate.set_confirm_callback(lambda *_args: next(responses))

    assert gate.check("command", {"action": "echo once"}) == (True, "")
    assert gate.state is PermissionState.APPROVED
    assert gate.check("command", {"action": "echo no"}) == (False, "用户拒绝")
    assert gate.state is PermissionState.DENIED
    assert gate.check("command", {"action": "echo cancel"}) == (False, "用户取消任务")
    assert gate.state is PermissionState.CANCELLED


def test_session_decision_is_applied_by_the_gate():
    gate = PermissionGate(PermissionMode.DEFAULT)
    gate.set_confirm_callback(lambda *_args: PermissionDecision.ALLOW_SESSION)

    assert gate.check("write_file", {"file_path": "a.txt"}) == (True, "")
    assert gate.check("write_file", {"file_path": "b.txt"}) == (True, "")
    assert gate.state is PermissionState.APPROVED


def test_session_decision_allows_later_critical_tool_calls():
    """Interactive [a] means this tool is trusted for the rest of the session."""
    gate = PermissionGate(PermissionMode.DEFAULT)
    gate.set_confirm_callback(lambda *_args: PermissionDecision.ALLOW_SESSION)

    assert gate.check("command", {"action": "echo first"}) == (True, "")
    assert gate.check("command", {"action": "ss -tlnp | grep 23119"}) == (True, "")
    assert gate.session_allowed_tools == ("command",)


def test_permission_mode_change_revokes_session_approval():
    gate = PermissionGate(PermissionMode.DEFAULT)
    gate.allow_always("command")
    gate.set_mode(PermissionMode.PLAN)

    allowed, reason = gate.check("command", {"action": "echo blocked"})
    assert allowed is False
    assert "PLAN 模式禁止" in reason
    assert gate.session_allowed_tools == ()


def test_cancel_decision_marks_context_and_tool_result_cancelled():
    gate = PermissionGate(PermissionMode.DEFAULT)
    gate.set_confirm_callback(lambda *_args: (False, "用户取消任务"))
    context = AgentContext()

    result = ToolExecutor(permission_gate=gate).execute(
        "command",
        {"action": "echo should-not-run"},
        context,
    )

    assert result.success is False
    assert result.cancelled is True
    assert context.get("_task_cancelled") is True
    assert gate.state is PermissionState.CANCELLED


def test_react_stops_without_a_second_model_turn_after_cancel(monkeypatch):
    gate = PermissionGate(PermissionMode.DEFAULT)
    gate.set_confirm_callback(lambda *_args: (False, "用户取消任务"))
    callback = SilentCallback()
    engine = ReActEngine(
        ["test/model"],
        tools={"command": BUILTIN_TOOLS["command"]},
        callback=callback,
        native_fc=False,
        permission_gate=gate,
    )
    responses = iter([
        '{"thought":"execute","action":"command","action_input":{"action":"echo hi"}}',
        '{"final_answer":"this must not be requested"}',
    ])
    calls = {"count": 0}

    def fake_llm(*_args, **_kwargs):
        calls["count"] += 1
        return next(responses)

    monkeypatch.setattr(engine, "_call_llm_for_phase", fake_llm)
    result = engine.run("执行命令 echo hi", context=AgentContext())

    assert "用户取消任务" in result
    assert calls["count"] == 1


def test_command_confirmation_displays_normalized_action_and_escapes_markup():
    message = PermissionGate.format_confirm_message(
        "command",
        {"action": "find /tmp -name '[abc]*'"},
        "CRITICAL",
    )

    assert "find /tmp" in message
    assert "命令: ?" not in message
    assert r"\[abc]" in message
    assert "本会话总是允许此工具" in message


@pytest.mark.parametrize("risk", ["WRITE", "CRITICAL"])
def test_confirmation_key_hints_survive_real_rich_panel_rendering(risk):
    message = PermissionGate.format_confirm_message(
        "write_file" if risk == "WRITE" else "command",
        {"file_path": "/tmp/result.py", "action": "python result.py"},
        risk,
    )
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    console.print(Panel(message))

    rendered = output.getvalue()
    assert "[y] 确认" in rendered
    assert "[n] 拒绝" in rendered
    assert "[a]" in rendered
    assert "[q] 取消任务" in rendered


def test_confirmation_message_shows_generic_tool_params_without_secrets():
    message = PermissionGate.format_confirm_message(
        "batch_write",
        {
            "files": [{"path": "a.py", "content": "secret-value"}],
            "token": "should-not-print",
        },
        "WRITE",
    )

    assert '"files"' in message
    assert "should-not-print" not in message
    assert "masked" in message


def test_critical_exact_approval_does_not_allow_a_different_command():
    gate = PermissionGate(PermissionMode.DEFAULT)
    first = {"action": "find /tmp -type f"}
    second = {"action": "touch /tmp/new-file"}
    gate.allow_exact("command", first)

    assert gate.check("command", first) == (True, "")
    allowed, reason = gate.check("command", second)
    assert allowed is False
    assert "需要确认" in reason


def test_plan_mode_denies_all_mutating_tools_without_prompt():
    gate = PermissionGate(PermissionMode.PLAN)

    for tool, params in [
        ("write_file", {"file_path": "result.txt"}),
        ("refactor", {"file_path": "main.py"}),
        ("clone_repo", {"repo": "owner/repo"}),
        ("git", {"git_command": "add ."}),
        ("command", {"action": "touch result.txt"}),
        ("mcp_call", {"tool_name": "external:write"}),
    ]:
        allowed, reason = gate.check(tool, params)
        assert allowed is False, tool
        assert "PLAN 模式禁止" in reason


@pytest.mark.parametrize("git_command", ["status", "diff", "log", "branch"])
def test_read_only_git_commands_are_allowed_in_plan_mode(git_command):
    gate = PermissionGate(PermissionMode.PLAN)

    assert gate.check("git", {"git_command": git_command}) == (True, "")


@pytest.mark.parametrize(
    "git_command",
    ["push origin main", "reset --hard HEAD", "clean -fd", "pull --rebase"],
)
def test_dangerous_git_uses_git_command_and_requires_critical_confirmation(git_command):
    seen = []
    gate = PermissionGate(PermissionMode.DEFAULT)
    gate.set_confirm_callback(
        lambda tool, params, risk: (seen.append((tool, params, risk)) or (False, "denied"))
    )

    allowed, reason = gate.check("git", {"git_command": git_command})

    assert (allowed, reason) == (False, "denied")
    assert seen[0][2] == "CRITICAL"


def test_tool_executor_enforces_plan_mode_before_file_write(tmp_path):
    target = tmp_path / "blocked.txt"
    executor = ToolExecutor(permission_gate=PermissionGate(PermissionMode.PLAN))

    result = executor.execute(
        "write_file",
        {"file_path": str(target), "content": "must not be written"},
        AgentContext(),
    )

    assert result.success is False
    assert "PLAN 模式禁止" in result.observation
    assert not target.exists()


def test_dynamic_tools_are_classified_as_sensitive(monkeypatch):
    from xenon.nodes import tool_node

    called = []
    monkeypatch.setitem(
        tool_node._DYNAMIC_TOOLS,
        "custom_side_effect",
        {
            "handler": lambda context: called.append(context) or {"success": True},
            "description": "test",
            "params": {},
        },
    )

    assert classify_tool("custom_side_effect") == "SENSITIVE"

    seen_risks = []
    gate = PermissionGate(PermissionMode.DEFAULT)
    gate.set_confirm_callback(
        lambda tool, params, risk: (seen_risks.append(risk) or (False, "denied"))
    )
    result = ToolExecutor(permission_gate=gate).execute(
        "custom_side_effect", {}, AgentContext()
    )

    assert result.success is False
    assert seen_risks == ["CRITICAL"]
    assert called == []


@pytest.mark.parametrize(
    ("module_name", "class_name", "runner_name"),
    [
        ("xenon.engine.react_engine", "ReActEngine", "_run_react_engine"),
        (
            "xenon.engine.plan_execute_engine",
            "PlanExecuteEngine",
            "_run_plan_execute_engine",
        ),
        (
            "xenon.engine.reflection_engine",
            "ReflectionEngine",
            "_run_reflection_engine",
        ),
        (
            "xenon.engine.combined_engines",
            "PlanReactEngine",
            "_run_plan_react_engine",
        ),
        (
            "xenon.engine.combined_engines",
            "PlanReflectionEngine",
            "_run_plan_reflection_engine",
        ),
        (
            "xenon.engine.combined_engines",
            "ReactReflectionEngine",
            "_run_react_reflection_engine",
        ),
    ],
)
def test_every_repl_engine_receives_the_live_permission_gate(
    monkeypatch, module_name, class_name, runner_name
):
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.repl import REPL

    captured = {}

    class FakeEngine:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def run(self, *args, **kwargs):
            return "ok"

    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, class_name, FakeEngine)

    registry = ModelRegistry()
    registry.add_model("openai/gpt-4o", "gpt4")
    repl = REPL(registry=registry, streaming=False)
    monkeypatch.setattr(repl, "_start_log_capture", lambda: None)
    monkeypatch.setattr(repl, "_stop_log_capture", lambda: "")
    monkeypatch.setattr(repl, "_render_engine_result", lambda *args, **kwargs: None)

    getattr(repl, runner_name)("test", ["openai/gpt-4o"])

    assert captured["permission_gate"] is repl._permission_gate
