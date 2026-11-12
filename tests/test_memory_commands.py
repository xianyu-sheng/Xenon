"""Regression tests for the memory slash-command group boundary."""

from __future__ import annotations

from xenon.repl.command_groups.memory_cmd import (
    _cmd_memory,
    _cmd_memory_v2,
)
from xenon.repl.commands import (
    _cmd_memory as legacy_memory,
)
from xenon.repl.commands import (
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


def test_memory_v2_status_reports_storage_locations():
    """The v2 surface is a different command; this test used to call v1.

    It was named for ``_cmd_memory_v2`` but exercised ``_cmd_memory`` with an
    ``isinstance(str)`` assertion, so the v2 branch had no coverage at all.
    """

    from types import SimpleNamespace

    from xenon.repl.command_groups.memory_cmd import _cmd_memory_v2

    class _Registry:
        @staticmethod
        def get(scope):
            raise ValueError("no backend configured")

    service = SimpleNamespace(
        registry=_Registry(),
        policy=SimpleNamespace(
            max_item_tokens=1, max_leaf_tokens=2, max_active_tokens=3
        ),
    )
    repl = SimpleNamespace(_get_memory_service=lambda: service)

    text = _cmd_memory_v2(args="status", repl=repl)

    assert "Memory v2" in text
    assert "存储位置" in text
