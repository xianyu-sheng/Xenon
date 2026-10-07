"""TargetResolver: 模型给的路径必须在围栏之前解析成真实路径。

回归事故：``~/Desktop/quicksort.py`` 曾在 Windows 下创建了字面量 ``~``
目录 ``C:\\Users\\Administrator\\~\\Desktop\\quicksort.py``，并被工作记忆
当成"已验证事实"反复喂回上下文。本文件锁住修复：``~``、环境变量、中文
文件夹别名（桌面/下载/文档/主目录/项目根）统一解析；字面量 ``~`` 组件
在围栏层被直接拒绝。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xenon.engine.context import AgentContext
from xenon.nodes.path_targets import resolve_target_path
from xenon.nodes.tool_node import SecurityError, ToolNode

HOME = Path.home() / "xenon-test-home"
CWD = Path.home() / "xenon-test-cwd"


# ── 解析层 ─────────────────────────────────────────────────


def test_tilde_expands_to_the_real_home():
    assert (
        resolve_target_path("~/Desktop/quicksort.py", cwd=CWD, home=HOME)
        == HOME / "Desktop" / "quicksort.py"
    )
    assert (
        resolve_target_path(r"~\Desktop\quicksort.py", cwd=CWD, home=HOME)
        == HOME / "Desktop" / "quicksort.py"
    )
    assert resolve_target_path("~", cwd=CWD, home=HOME) == HOME


def test_language_folder_aliases():
    assert (
        resolve_target_path("桌面/quicksort.py", cwd=CWD, home=HOME)
        == HOME / "Desktop" / "quicksort.py"
    )
    assert (
        resolve_target_path("我的文档/note.md", cwd=CWD, home=HOME)
        == HOME / "Documents" / "note.md"
    )
    assert resolve_target_path("下载", cwd=CWD, home=HOME) == HOME / "Downloads"
    assert resolve_target_path("主目录/repo", cwd=CWD, home=HOME) == HOME / "repo"
    assert (
        resolve_target_path("项目根/src/app.py", cwd=CWD, home=HOME)
        == CWD / "src" / "app.py"
    )
    assert resolve_target_path("当前目录/x.py", cwd=CWD, home=HOME) == CWD / "x.py"


def test_environment_variables_and_wrapping_quotes(monkeypatch):
    monkeypatch.setenv("XENON_TEST_DIR", str(HOME))
    assert (
        resolve_target_path("%XENON_TEST_DIR%/Desktop/a.py", cwd=CWD, home=HOME)
        == HOME / "Desktop" / "a.py"
    )
    assert (
        resolve_target_path('"桌面/quicksort.py"', cwd=CWD, home=HOME)
        == HOME / "Desktop" / "quicksort.py"
    )
    assert (
        resolve_target_path("`桌面/quicksort.py`。", cwd=CWD, home=HOME)
        == HOME / "Desktop" / "quicksort.py"
    )


def test_relative_paths_still_join_the_cwd():
    assert (
        resolve_target_path("src/app.py", cwd=CWD, home=HOME) == CWD / "src" / "app.py"
    )


def test_literal_tilde_component_is_rejected():
    with pytest.raises(SecurityError, match="字面量"):
        resolve_target_path("foo/~/bar.py", cwd=CWD, home=HOME)
    with pytest.raises(SecurityError, match="字面量"):
        resolve_target_path("C:/x/~/y.py", cwd=CWD, home=HOME)


def test_empty_target_is_rejected():
    with pytest.raises(SecurityError):
        resolve_target_path("   ", cwd=CWD, home=HOME)


# ── 围栏集成 ───────────────────────────────────────────────


def test_validate_path_resolves_home_inside_the_fence(
    real_path_validation, monkeypatch, tmp_path
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr("xenon.nodes.path_targets._path_home", lambda: workspace)
    node = ToolNode("w", action_type="write_file", cwd=str(workspace))

    resolved = node._validate_path("~/Desktop/quicksort.py", for_write=True)

    assert resolved == (workspace / "Desktop" / "quicksort.py").resolve()
    assert "~" not in resolved.parts


def test_validate_path_rejects_literal_tilde_component(
    real_path_validation, monkeypatch, tmp_path
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    node = ToolNode("w", action_type="write_file", cwd=str(workspace))

    with pytest.raises(SecurityError, match="字面量"):
        node._validate_path("foo/~/quicksort.py", for_write=True)


def test_write_file_lands_in_the_real_folder_not_literal_tilde(
    real_path_validation, monkeypatch, tmp_path
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr("xenon.nodes.path_targets._path_home", lambda: workspace)

    result = ToolNode(
        "write",
        action_type="write_file",
        cwd=str(workspace),
        file_path="~/Desktop/quicksort.py",
        content="print('hi')\n",
    ).execute(AgentContext())

    assert result["success"] is True, result
    assert (workspace / "Desktop" / "quicksort.py").read_text() == "print('hi')\n"
    assert not (workspace / "~").exists()
