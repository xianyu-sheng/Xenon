from __future__ import annotations

import sys
from pathlib import Path

from omniagent.engine.context import AgentContext
from omniagent.repl.context_manager import ContextManager
from omniagent.repl.file_links import (
    build_editor_command,
    format_file_link,
    linkify_file_paths,
    parse_file_target,
)
from omniagent.repl.model_registry import ModelRegistry
from omniagent.repl.shell_runner import run_shell_command
from omniagent.repl.terminal_bridge import TerminalBridge


def test_parse_file_target_with_windows_drive_and_line():
    target = parse_file_target(r"C:\repo\src\main.py:42:7")

    assert str(target.path).endswith(r"C:\repo\src\main.py")
    assert target.line == 42
    assert target.column == 7


def test_format_and_linkify_file_paths(tmp_path: Path):
    file_path = tmp_path / "README.md"
    file_path.write_text("# demo", encoding="utf-8")

    link = format_file_link(str(file_path))
    linked = linkify_file_paths(f"See {file_path}:1", cwd=tmp_path)

    assert "[link=file:///" in link
    assert "[link=file:///" in linked
    assert str(file_path) in linked


def test_build_editor_command_prefers_configured_editor(tmp_path: Path):
    target = parse_file_target("src/app.py:12", cwd=tmp_path)
    command = build_editor_command(target, editor="code -g {file}:{line}:{column}")

    assert command[:2] == ["code", "-g"]
    assert command[2].endswith("src\\app.py:12:1") or command[2].endswith("src/app.py:12:1")


def test_terminal_bridge_builds_windows_terminal_split_command(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda name: "wt.exe" if name in {"wt.exe", "wt"} else None)

    bridge = TerminalBridge(root=tmp_path)
    command, mode = bridge.build_launch_command(cwd=tmp_path, log_path=tmp_path / "terminal.log")

    assert mode == "windows-terminal-split-pane"
    assert command[:5] == ["wt.exe", "-w", "0", "split-pane", "-H"]
    assert "Start-Transcript" in command[-1]


def test_terminal_bridge_falls_back_after_split_launch_failure(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda name: "wt.exe" if name in {"wt.exe", "wt"} else None)

    bridge = TerminalBridge(root=tmp_path)
    calls = []

    def fake_launch(command, mode):
        calls.append(mode)
        if mode == "windows-terminal-split-pane":
            raise OSError("wt failed")

    monkeypatch.setattr(bridge, "_launch", fake_launch)
    result = bridge.open_terminal(cwd=tmp_path)

    assert result.success is True
    assert calls == ["windows-terminal-split-pane", "powershell-new-window"]
    assert result.session is not None
    assert result.session.mode == "powershell-new-window"


def test_terminal_bridge_status_and_tail(monkeypatch, tmp_path: Path):
    bridge = TerminalBridge(root=tmp_path)
    monkeypatch.setattr(bridge, "_launch", lambda command, mode: None)
    result = bridge.open_terminal(cwd=tmp_path)

    assert result.success is True
    assert bridge.session is not None

    bridge.session.log_path.write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
    assert bridge.read_tail(lines=2) == "line 2\nline 3"
    assert "line 3" in bridge.status(lines=2)


def test_shell_command_runs_through_tool_node(tmp_path: Path):
    command = "python -c \"print(12345)\""
    result = run_shell_command(command, cwd=tmp_path, context=AgentContext())

    assert result.success is True
    assert "12345" in result.stdout


def test_shell_command_rejects_dangerous_command(tmp_path: Path):
    result = run_shell_command("shutdown /s /t 0", cwd=tmp_path, context=AgentContext())

    assert result.success is False
    assert "危险命令被拦截" in result.error


def test_repl_shell_input_detection():
    from omniagent.repl.repl import REPL

    assert REPL._is_shell_input("!python -V") is True
    assert REPL._extract_shell_command("!python -V") == "python -V"
    assert REPL._is_shell_input("!= value") is False


def test_prompt_toolkit_key_bindings_can_be_created():
    from omniagent.repl.repl import REPL

    bindings = REPL._prompt_key_bindings()

    assert bindings is not None


def test_shell_command_dispatch_updates_context(tmp_path: Path):
    from omniagent.repl.commands import dispatch_command

    registry = ModelRegistry()
    ctx_mgr = ContextManager()
    agent_context = AgentContext()
    session_state = {"agent_context": agent_context}
    command = "python -c \"print(67890)\""

    output = dispatch_command(
        "/shell",
        command,
        registry=registry,
        ctx_mgr=ctx_mgr,
        session_state=session_state,
    )

    assert output is not None
    assert "67890" in output
    assert agent_context.get("_last_shell_command")
    assert "67890" in agent_context.get("_last_shell_output")
    assert len(ctx_mgr.history) == 2


def test_terminal_quote_adds_tail_to_context(monkeypatch, tmp_path: Path):
    from omniagent.repl.commands import dispatch_command

    bridge = TerminalBridge(root=tmp_path)
    monkeypatch.setattr(bridge, "_launch", lambda command, mode: None)
    result = bridge.open_terminal(cwd=tmp_path)
    assert result.session is not None
    result.session.log_path.write_text("error: failed at tests/test_demo.py:9\n", encoding="utf-8")

    registry = ModelRegistry()
    ctx_mgr = ContextManager()
    agent_context = AgentContext()
    session_state = {"_terminal_bridge": bridge, "agent_context": agent_context}

    output = dispatch_command(
        "/terminal_quote",
        "5",
        registry=registry,
        ctx_mgr=ctx_mgr,
        session_state=session_state,
    )

    assert output is not None
    assert "已引用" in output
    assert "tests/test_demo.py:9" in ctx_mgr.history[-1].content
    assert "tests/test_demo.py:9" in agent_context.get("_last_terminal_quote")
