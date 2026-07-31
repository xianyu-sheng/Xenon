"""Regression tests for the resources slash-command group boundary."""

from __future__ import annotations

from xenon.repl.command_groups.resources import (
    _MCP_USAGE,
    _cmd_library,
    _cmd_mcp,
    _cmd_setup,
    _cmd_skill_discover,
    _cmd_skill_install,
    _cmd_status,
    _cmd_tools,
)
from xenon.repl.commands import (
    _MCP_USAGE as legacy_mcp_usage,
    _cmd_library as legacy_library,
    _cmd_mcp as legacy_mcp,
    _cmd_setup as legacy_setup,
    _cmd_skill_discover as legacy_skill_discover,
    _cmd_skill_install as legacy_skill_install,
    _cmd_status as legacy_status,
    _cmd_tools as legacy_tools,
)


def test_resources_group_preserves_legacy_command_exports():
    assert legacy_mcp_usage is _MCP_USAGE
    assert legacy_library is _cmd_library
    assert legacy_mcp is _cmd_mcp
    assert legacy_setup is _cmd_setup
    assert legacy_skill_discover is _cmd_skill_discover
    assert legacy_skill_install is _cmd_skill_install
    assert legacy_status is _cmd_status
    assert legacy_tools is _cmd_tools


def test_mcp_usage_is_string():
    assert isinstance(_MCP_USAGE, str)
    assert "MCP 使用指南" in _MCP_USAGE


def test_mcp_no_repl():
    result = _cmd_mcp(args="", session_state={})
    assert "无法获取 REPL 状态" in result


def test_library_no_args():
    result = _cmd_library(args="")
    assert "库刷新结果" in result


def test_skill_discover_no_keyword():
    result = _cmd_skill_discover(args="")
    assert "Skill 库" in result


def test_skill_install_no_name():
    result = _cmd_skill_install(args="")
    assert "用法" in result


def test_status_basic():
    from xenon.repl.context_manager import ContextManager
    from xenon.repl.model_registry import ModelRegistry

    ctx = ContextManager()
    reg = ModelRegistry()
    result = _cmd_status(ctx_mgr=ctx, registry=reg, session_state={})
    assert "系统状态" in result


def test_setup_no_repl():
    result = _cmd_setup(session_state={})
    assert "无法获取 REPL 状态" in result


def test_tools_lists():
    result = _cmd_tools()
    assert "可用工具类型" in result
