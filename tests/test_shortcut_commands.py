"""Regression tests for the shortcut slash-command group boundary."""

from __future__ import annotations

from xenon.repl.command_groups.shortcut import (
    _cmd_shortcut,
    _generate_shortcut_steps,
    _parse_shortcut_steps,
    _shortcut_auto_generate,
    _shortcut_create_interactive,
    _shortcut_manual_create,
)
from xenon.repl.commands import (
    _cmd_shortcut as legacy_shortcut,
)
from xenon.repl.commands import (
    _generate_shortcut_steps as legacy_generate,
)
from xenon.repl.commands import (
    _parse_shortcut_steps as legacy_parse,
)
from xenon.repl.commands import (
    _shortcut_auto_generate as legacy_auto_gen,
)
from xenon.repl.commands import (
    _shortcut_create_interactive as legacy_create,
)
from xenon.repl.commands import (
    _shortcut_manual_create as legacy_manual,
)


def test_shortcut_group_preserves_legacy_command_exports():
    assert legacy_shortcut is _cmd_shortcut
    assert legacy_generate is _generate_shortcut_steps
    assert legacy_parse is _parse_shortcut_steps
    assert legacy_auto_gen is _shortcut_auto_generate
    assert legacy_create is _shortcut_create_interactive
    assert legacy_manual is _shortcut_manual_create


def test_shortcut_list_no_repl():
    result = _cmd_shortcut(args="list", registry=None, session_state={})
    assert isinstance(result, str)


def test_shortcut_run_no_args():
    result = _cmd_shortcut(args="run", registry=None, session_state={})
    assert "用法" in result


def test_shortcut_delete_no_args():
    result = _cmd_shortcut(args="delete", registry=None, session_state={})
    assert "用法" in result


def test_parse_shortcut_steps_empty():
    result = _parse_shortcut_steps("not json")
    assert result == []


def test_parse_shortcut_steps_list():
    result = _parse_shortcut_steps('["cmd1", "cmd2"]')
    assert result == ["cmd1", "cmd2"]


def test_generate_shortcut_no_model():
    result = _generate_shortcut_steps("test", registry=None)
    assert result == []


def test_a_created_shortcut_is_callable_without_a_restart(monkeypatch):
    """The creation message promises ``/<name>`` works immediately.

    Measured through the real front-end: ``/shortcut create`` printed
    "使用 /e2e-hello 执行", and the very next keystroke answered
    "未知命令: /e2e-hello" because the dynamic handler was only wired at REPL
    startup.  Skills already registered on creation; shortcuts now do too.
    """

    import types

    import rich.prompt as rich_prompt

    from xenon.repl.command_registry import _HANDLERS, COMMANDS, dispatch_command

    name = "e2e-created-now"
    calls: list[tuple[str, str]] = []

    class _Manager:
        def create(self, name, description, steps):
            return types.SimpleNamespace(name=name, description=description)

        def execute(self, sc_name, args=""):
            calls.append((sc_name, args))
            return "ran"

    answers = iter(
        [name, "回显测试", "echo hi", "END"]
    )

    def fake_ask(prompt, **kwargs):
        return next(answers)

    monkeypatch.setattr(rich_prompt.Prompt, "ask", staticmethod(fake_ask))
    try:
        reply = _shortcut_manual_create(name, "回显测试", _Manager())
        assert "已创建" in reply
        assert f"/{name}" in _HANDLERS
        assert (
            dispatch_command(
                f"/{name}", "", registry=None, ctx_mgr=None, session_state={}
            )
            == "ran"
        )
        assert calls == [(name, "")]
    finally:
        _HANDLERS.pop(f"/{name}", None)
        COMMANDS.pop(f"/{name}", None)

