"""Regression tests for the workspace slash-command group boundary."""

from __future__ import annotations

from xenon.repl.command_groups.workspace import (
    _cmd_edit as grouped_cmd_edit,
)
from xenon.repl.command_groups.workspace import (
    _cmd_project as grouped_cmd_project,
)
from xenon.repl.command_groups.workspace import (
    _cmd_vision as grouped_cmd_vision,
)
from xenon.repl.commands import (
    _cmd_edit,
    _cmd_permissions,
    _cmd_project,
    _cmd_vision,
)
from xenon.repl.model_registry import ModelRegistry


def test_workspace_group_preserves_legacy_command_exports():
    assert _cmd_project is grouped_cmd_project
    assert _cmd_edit is grouped_cmd_edit
    assert _cmd_vision is grouped_cmd_vision


def test_permissions_is_owned_by_the_grants_module_not_workspace():
    """/permissions used to be registered twice; the later import won and #373's
    grants view was unreachable.  One owner, asserted by identity."""
    import xenon.repl.command_groups.workspace as workspace
    from xenon.repl.command_groups.permissions import (
        _cmd_permissions as owned,
    )

    assert _cmd_permissions is owned
    assert not hasattr(workspace, "_cmd_permissions")


def test_workspace_commands_keep_no_repl_fallbacks():
    assert "无法访问 REPL" in _cmd_project(args="", session_state={})
    assert "用法" in _cmd_edit(args="", registry=ModelRegistry())
    assert "需要交互会话" in _cmd_permissions(args="", session_state={})
    assert "REPL 未初始化" in _cmd_vision(args="", session_state={})
