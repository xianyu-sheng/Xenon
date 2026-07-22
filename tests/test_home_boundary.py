"""Privacy boundary regressions for launching Xenon from HOME."""

from __future__ import annotations

import io
import os

import pytest
from rich.console import Console

from xenon.memory import MemoryBackendRegistry, MemoryKind, MemoryScope, MemoryService
from xenon.repl.commands import _cmd_memory_v2
from xenon.repl.project_context import ProjectContext
from xenon.repl.repl import REPL


def test_home_is_unscoped_even_when_it_contains_project_markers(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".git").mkdir()
    (home / "pyproject.toml").write_text("[project]\nname='dotfiles'")
    (home / "private-notes.txt").write_text("must never enter context")
    global_root = tmp_path / "config" / "xenon"
    global_root.mkdir(parents=True)
    (global_root / "XENON.md").write_text("全局回答规则", encoding="utf-8")
    monkeypatch.setattr(
        os.path,
        "expanduser",
        lambda value: str(home) if value == "~" else value,
    )

    context = ProjectContext(global_config_root=global_root)
    found = context.detect(home)

    assert found is False
    assert context.root is None
    assert context.file_tree == ""
    assert context.key_files == {}
    assert "全局回答规则" in context.rules
    rendered = context.format_for_context()
    assert "用户全局指令" in rendered
    assert "private-notes.txt" not in rendered
    assert "pyproject.toml" not in rendered
    assert "安全的无项目模式" in context.get_summary()


def test_home_markers_are_not_inherited_by_a_markerless_child(tmp_path, monkeypatch):
    home = tmp_path / "home"
    scratch = home / "scratch"
    scratch.mkdir(parents=True)
    (scratch / "visible.txt").write_text("bounded")
    (home / "package.json").write_text('{"name":"home-tooling"}')
    (home / "private.txt").write_text("secret filename")
    monkeypatch.setattr(
        os.path,
        "expanduser",
        lambda value: str(home) if value == "~" else value,
    )

    context = ProjectContext(global_config_root=tmp_path / "missing")
    found = context.detect(scratch)

    assert found is False
    assert context.root == scratch.resolve()
    assert "visible.txt" in context.file_tree
    assert "private.txt" not in context.file_tree
    assert "package.json" not in context.key_files


def test_unscoped_memory_registry_exposes_only_user_memory(tmp_path):
    registry = MemoryBackendRegistry(
        None,
        user_data_root=tmp_path / "user-memory",
        user_config_root=tmp_path / "user-config",
    )
    service = MemoryService(registry)

    assert registry.has_project is False
    assert registry.persistent_scopes() == (MemoryScope.USER,)
    with pytest.raises(ValueError, match="当前未检测到项目"):
        registry.get(MemoryScope.PROJECT_LOCAL)

    receipt = service.remember(
        "用户偏好简洁输出",
        scope=MemoryScope.USER,
        kind=MemoryKind.PREFERENCE,
    )

    assert receipt.record.scope is MemoryScope.USER
    assert (tmp_path / "user-memory" / "metadata.json").exists()
    assert not (tmp_path / ".xenon").exists()
    with pytest.raises(ValueError, match="当前未检测到项目"):
        service.remember("项目事实", scope=MemoryScope.PROJECT_LOCAL)


def test_plain_remember_in_unscoped_mode_defaults_to_user_global(tmp_path, monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(
        "xenon.repl.repl.console",
        Console(file=output, width=100, force_terminal=False),
    )
    repl = REPL(streaming=False)
    repl.project_ctx.root = None
    repl.project_ctx._initialized = True
    repl._memory_service = MemoryService(MemoryBackendRegistry(
        None,
        user_data_root=tmp_path / "user-memory",
        user_config_root=tmp_path / "user-config",
    ))

    handled = repl._handle_explicit_memory_request("记住：我喜欢简洁输出")

    assert handled is True
    records = repl._memory_service.list_records()
    assert len(records) == 1
    assert records[0].scope is MemoryScope.USER
    assert "默认写入用户全局记忆" in output.getvalue()


def test_explicit_project_memory_is_rejected_without_a_project(tmp_path, monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(
        "xenon.repl.repl.console",
        Console(file=output, width=100, force_terminal=False),
    )
    repl = REPL(streaming=False)
    repl.project_ctx.root = None
    repl.project_ctx._initialized = True
    repl._memory_service = MemoryService(MemoryBackendRegistry(
        None,
        user_data_root=tmp_path / "user-memory",
        user_config_root=tmp_path / "user-config",
    ))

    handled = repl._handle_explicit_memory_request(
        "记住：项目使用 Python 3.12，存到项目本地记忆"
    )

    assert handled is True
    assert repl._memory_service.list_records() == []
    assert "当前未检测到项目" in output.getvalue()


def test_memory_status_marks_project_scopes_inactive_without_project(tmp_path):
    repl = REPL(streaming=False)
    repl._memory_service = MemoryService(MemoryBackendRegistry(
        None,
        user_data_root=tmp_path / "user-memory",
        user_config_root=tmp_path / "user-config",
    ))

    result = _cmd_memory_v2(args="status", repl=repl)

    assert "project-local: 未激活（当前未检测到项目）" in result
    assert "project-shared: 未激活（当前未检测到项目）" in result


def test_memory_add_defaults_to_user_scope_without_project(tmp_path):
    repl = REPL(streaming=False)
    repl._memory_service = MemoryService(MemoryBackendRegistry(
        None,
        user_data_root=tmp_path / "user-memory",
        user_config_root=tmp_path / "user-config",
    ))

    result = _cmd_memory_v2(args="add 默认使用简洁输出", repl=repl)

    records = repl._memory_service.list_records()
    assert len(records) == 1
    assert records[0].scope is MemoryScope.USER
    assert "范围: user" in result
    assert str(tmp_path / "user-memory") in result
