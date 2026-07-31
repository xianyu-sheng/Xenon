"""Regression tests for the session slash-command group boundary."""

from __future__ import annotations

import pytest

from xenon.repl.command_groups.session import (
    _cmd_clear,
    _cmd_compact,
    _cmd_config,
    _cmd_context,
    _cmd_help,
    _cmd_history,
    _cmd_load,
    _cmd_resume,
    _cmd_save,
    _cmd_sessions,
    _cmd_undo,
)
from xenon.repl.commands import (
    _cmd_clear as legacy_clear,
    _cmd_compact as legacy_compact,
    _cmd_config as legacy_config,
    _cmd_context as legacy_context,
    _cmd_exit as legacy_exit,
    _cmd_help as legacy_help,
    _cmd_history as legacy_history,
    _cmd_load as legacy_load,
    _cmd_resume as legacy_resume,
    _cmd_save as legacy_save,
    _cmd_sessions as legacy_sessions,
    _cmd_undo as legacy_undo,
)


def test_session_group_preserves_legacy_command_exports():
    """Verify that the re-exported names in commands.py point to the real
    implementations in command_groups.session."""
    assert legacy_clear is _cmd_clear
    assert legacy_compact is _cmd_compact
    assert legacy_config is _cmd_config
    assert legacy_context is _cmd_context
    assert legacy_help is _cmd_help
    assert legacy_history is _cmd_history
    assert legacy_load is _cmd_load
    assert legacy_resume is _cmd_resume
    assert legacy_save is _cmd_save
    assert legacy_sessions is _cmd_sessions
    assert legacy_undo is _cmd_undo


def test_legacy_exit_is_still_reachable():
    """_cmd_exit must still raise ExitSignal correctly."""
    from xenon.repl.command_registry import ExitSignal

    assert legacy_exit is not None
    with pytest.raises(ExitSignal):
        legacy_exit()


def test_help_no_args_lists_commands():
    result = _cmd_help(args="")
    assert "可用命令" in result


def test_help_unknown_command():
    result = _cmd_help(args="nonexistent_cmd")
    assert "未知命令" in result


def test_sessions_no_repl():
    result = _cmd_sessions()
    # Without a REPL session, should just try to list — will return
    # either empty or list of saved sessions
    assert isinstance(result, str)


def test_undo_no_ctx_mgr():
    from xenon.repl.context_manager import ContextManager

    ctx = ContextManager()
    result = _cmd_undo(ctx_mgr=ctx)
    assert "没有可回退的状态" in result


def test_clear_needs_confirm():
    """In non-interactive mode without XENON_ASSUME_YES, clear defaults to
    the command's default=True, so it should proceed."""
    from xenon.repl.context_manager import ContextManager

    ctx = ContextManager()
    result = _cmd_clear(ctx_mgr=ctx)
    # With default=True, non-interactive fallback proceeds
    assert "已清空" in result


def test_context_shows_stats():
    from xenon.repl.context_manager import ContextManager

    ctx = ContextManager()
    result = _cmd_context(ctx_mgr=ctx, session_state={})
    assert "消息总数" in result
    assert "估算 Token" in result


def test_config_no_args():
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    result = _cmd_config(args="", registry=reg)
    assert "当前配置" in result


def test_history_no_router():
    result = _cmd_history(args="", session_state={})
    assert "路由历史不可用" in result


def test_save_needs_name():
    from xenon.repl.context_manager import ContextManager
    from xenon.repl.model_registry import ModelRegistry

    ctx = ContextManager()
    reg = ModelRegistry()
    result = _cmd_save(args="", ctx_mgr=ctx, session_state={}, registry=reg)
    assert "用法" in result


def test_load_needs_name():
    from xenon.repl.context_manager import ContextManager
    from xenon.repl.model_registry import ModelRegistry

    ctx = ContextManager()
    reg = ModelRegistry()
    result = _cmd_load(args="", ctx_mgr=ctx, session_state={}, registry=reg)
    assert "用法" in result


def test_resume_no_repl():
    result = _cmd_resume(args="", session_state={})
    assert "REPL 实例不可用" in result or "没有已保存的会话" in result


def test_compact_no_args():
    from xenon.repl.context_manager import ContextManager
    from xenon.repl.model_registry import ModelRegistry

    ctx = ContextManager()
    reg = ModelRegistry()
    # compact may fail without model, just check it returns a string
    result = _cmd_compact(args="", ctx_mgr=ctx, registry=reg)
    assert isinstance(result, str)
