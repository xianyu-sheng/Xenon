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
    _generate_shortcut_steps as legacy_generate,
    _parse_shortcut_steps as legacy_parse,
    _shortcut_auto_generate as legacy_auto_gen,
    _shortcut_create_interactive as legacy_create,
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
