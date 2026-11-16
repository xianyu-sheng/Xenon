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


def test_edit_records_the_change_for_review(monkeypatch, tmp_path):
    """A successful LLM edit must land in session working memory.

    ``/review`` is the reader; before this, the only writer was a test helper,
    so the command answered "本会话没有记录到文件改动" right after an edit.
    """

    from xenon.repl.code_editor import CodeEditor
    from xenon.repl.context_manager import ContextManager
    from xenon.repl.model_registry import ModelRegistry

    registry = ModelRegistry()
    registry.add_model("vendor/live", "live")
    ctx = ContextManager()
    target = tmp_path / "a.py"
    target.write_text("print(1)\n", encoding="utf-8")

    monkeypatch.setattr(
        CodeEditor, "edit_with_llm", staticmethod(lambda *a, **k: "✅ 已修改 a.py")
    )

    result = _cmd_edit(
        args=f"{target} 改成打印 2",
        registry=registry,
        ctx_mgr=ctx,
    )

    assert result == "✅ 已修改 a.py"
    assert ctx.get_working_memory()["session_modified_files"] == [
        str(target.resolve())
    ]
