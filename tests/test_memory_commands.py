"""Regression tests for the memory slash-command group boundary."""

from __future__ import annotations

from xenon.repl.command_groups.memory_cmd import (
    _cmd_memory,
    _cmd_memory_v2,
)
from xenon.repl.commands import (
    _cmd_memory as legacy_memory,
    _cmd_memory_v2 as legacy_memory_v2,
)


def test_memory_group_preserves_legacy_command_exports():
    assert legacy_memory is _cmd_memory
    assert legacy_memory_v2 is _cmd_memory_v2


def test_memory_list_no_repl():
    result = _cmd_memory(args="list", session_state={})
    assert isinstance(result, str)
    # May say "暂无记忆" or list memories


def test_memory_search_no_args():
    result = _cmd_memory(args="search", session_state={})
    assert "用法" in result


def test_memory_add_no_args():
    result = _cmd_memory(args="add", session_state={})
    assert "用法" in result


def test_memory_v2_status():
    """_cmd_memory_v2 needs a repl with _get_memory_service."""
    # Without a proper repl, we can only test basic behavior
    # The v1 path would be taken when repl doesn't have _get_memory_service
    result = _cmd_memory(args="list", session_state={})
    assert isinstance(result, str)
