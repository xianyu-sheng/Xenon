"""Permission-gate integration tests for the real REPL execution path."""

from __future__ import annotations

import importlib

import pytest

from xenon.engine.context import AgentContext
from xenon.nodes.tool_executor import ToolExecutor, classify_tool
from xenon.repl.permissions import PermissionGate, PermissionMode


def test_default_gate_fails_closed_without_confirmation_callback():
    gate = PermissionGate(PermissionMode.DEFAULT)

    allowed, reason = gate.check("write_file", {"file_path": "result.txt"})

    assert allowed is False
    assert "需要确认" in reason


def test_command_confirmation_displays_normalized_action_and_escapes_markup():
    message = PermissionGate.format_confirm_message(
        "command",
        {"action": "find /tmp -name '[abc]*'"},
        "CRITICAL",
    )

    assert "find /tmp" in message
    assert "命令: ?" not in message
    assert r"\[abc]" in message
    assert "本会话允许相同操作" in message


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
        ("xenon.engine.novel_engine", "NovelEngine", "_run_novel_engine"),
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
