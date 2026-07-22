"""Regression tests for Xenon's interactive visual hierarchy."""

from __future__ import annotations

import io
import os

from rich.console import Console
from prompt_toolkit.keys import Keys

from xenon.repl.context_manager import ContextManager
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL
from xenon.repl.status_bar import StatusBar


def _bar() -> StatusBar:
    return StatusBar(Console(file=io.StringIO()), ContextManager(), ModelRegistry())


def test_toolbar_has_api_model_and_context_fragments():
    fragments = _bar().get_toolbar_fragments()
    assert fragments[0] == ("class:toolbar.danger", "  ○ API /setup")
    assert any(style == "class:toolbar.mode" for style, _ in fragments)
    assert any("context" in text for _, text in fragments)


def test_input_rule_spans_terminal_width(monkeypatch):
    monkeypatch.setattr(
        "xenon.repl.status_bar.shutil.get_terminal_size",
        lambda *a: os.terminal_size((48, 24)),
    )
    fragments = _bar().get_input_rule_fragments()
    assert fragments == [("class:input.rule", "─" * 47)]


def test_prompt_keeps_rule_with_input_and_status_at_screen_bottom():
    repl = REPL()
    assert repl._pt_session is not None
    assert repl._pt_session.bottom_toolbar == repl.status_bar.get_toolbar_fragments

    root = repl._pt_session.app.layout.container
    main = root.children[0]
    main_stack = main.alternative_content.content
    assert main_stack.children[-1].content.text == repl.status_bar.get_input_rule_fragments

    buffer_window = main_stack.children[1].content
    assert buffer_window.height() == 1


def test_ctrl_o_suspends_prompt_toolkit_before_rendering(monkeypatch):
    repl = REPL()
    calls = []

    def fake_run_in_terminal(callback):
        calls.append(callback)

    monkeypatch.setattr("xenon.repl.repl.run_in_terminal", fake_run_in_terminal)
    binding = next(
        item
        for item in repl._pt_session.key_bindings.bindings
        if item.keys == (Keys.ControlO,)
    )
    binding.handler(object())

    assert calls == [repl._toggle_thinking_details]


def test_shift_tab_suspends_prompt_toolkit_before_printing(monkeypatch):
    repl = REPL()
    calls = []

    monkeypatch.setattr(
        "xenon.repl.repl.run_in_terminal",
        lambda callback: calls.append(callback),
    )
    binding = next(
        item
        for item in repl._pt_session.key_bindings.bindings
        if item.keys == (Keys.BackTab,)
    )
    binding.handler(object())

    assert calls == [repl._handle_shift_tab]


def test_toolbar_promotes_compaction_warning():
    bar = _bar()
    bar.ctx_mgr.add_user_message("x" * 200_000)
    fragments = bar.get_toolbar_fragments()
    assert ("class:toolbar.danger", "⚠ /compact") in fragments


def test_assistant_and_optimized_prompt_render_without_panels(monkeypatch):
    output = io.StringIO()
    test_console = Console(file=output, width=100, force_terminal=False)
    monkeypatch.setattr("xenon.repl.repl.console", test_console)

    REPL._render_assistant_text("正常亮度的回复", model_id="deepseek/v4")
    REPL._render_secondary_text("📝 优化后的 Prompt", "辅助提示词")

    rendered = output.getvalue()
    assert "正常亮度的回复" in rendered
    assert "辅助提示词" in rendered
    assert "╭" not in rendered
    assert "╰" not in rendered


def test_welcome_uses_running_source_version(monkeypatch):
    from xenon import __version__

    output = io.StringIO()
    test_console = Console(file=output, width=100, force_terminal=False)
    monkeypatch.setattr("xenon.repl.repl.console", test_console)
    repl = REPL()

    repl._print_welcome()

    assert f"v{__version__}" in output.getvalue()
