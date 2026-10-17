"""D-2 回归：XENON_EXTRA_ROOTS 允许访问启动目录之外的仓库。

真实事故（2026-10-17 的 Xenon 会话）：用户从 C:\\Users\\Administrator 启动，
仓库在 D:\\SmartBench，所有文件工具都报「路径越界」。修复后可用
XENON_EXTRA_ROOTS 显式追加允许目录（默认行为不变）。
"""

from __future__ import annotations

import os

import pytest

from xenon.nodes.tool_node import SecurityError, ToolNode


def test_outside_path_denied_without_env(
    tmp_path, monkeypatch, real_path_validation
) -> None:
    monkeypatch.delenv("XENON_EXTRA_ROOTS", raising=False)
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "a.txt").write_text("x", encoding="utf-8")

    node = ToolNode(
        "t",
        action_type="read_file",
        file_path=str(outside / "a.txt"),
        cwd=str(workspace),
    )
    with pytest.raises(SecurityError, match="路径越界"):
        node._validate_path(str(outside / "a.txt"))


def test_env_extra_roots_allows_outside_path(
    tmp_path, monkeypatch, real_path_validation
) -> None:
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    target = outside / "a.txt"
    target.write_text("x", encoding="utf-8")

    monkeypatch.setenv("XENON_EXTRA_ROOTS", str(outside))
    node = ToolNode(
        "t",
        action_type="read_file",
        file_path=str(target),
        cwd=str(workspace),
    )
    assert node._validate_path(str(target)) == target.resolve()


def test_env_extra_roots_accepts_multiple_entries(
    tmp_path, monkeypatch, real_path_validation
) -> None:
    workspace = tmp_path / "ws"
    first = tmp_path / "first"
    second = tmp_path / "second"
    for path in (workspace, first, second):
        path.mkdir()
    target = second / "b.txt"
    target.write_text("x", encoding="utf-8")

    monkeypatch.setenv(
        "XENON_EXTRA_ROOTS", os.pathsep.join([str(first), str(second)])
    )
    node = ToolNode(
        "t",
        action_type="read_file",
        file_path=str(target),
        cwd=str(workspace),
    )
    assert node._validate_path(str(target)) == target.resolve()


def test_env_extra_roots_ignores_blank_entries(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("XENON_EXTRA_ROOTS", f"{os.pathsep}{os.pathsep}")

    roots = ToolNode._extra_allowed_roots()
    assert isinstance(roots, tuple)
    # 不因空条目抛异常；工作区路径仍在根路径里
    node = ToolNode("t", action_type="read_file", cwd=str(workspace))
    assert node._get_allowed_root() == workspace.resolve()
