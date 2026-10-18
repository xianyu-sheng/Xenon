"""clone_repo 默认缓存根：工作区外时回退到工作区，保证克隆可读。"""

from __future__ import annotations

from pathlib import Path

import pytest

from xenon.nodes import tool_node as tool_node_module
from xenon.nodes.tool_node import ToolNode


@pytest.fixture(autouse=True)
def _no_default_extra_roots(monkeypatch):
    # Linux 上 pytest 的 tmp_path 位于 /tmp（默认允许根）下；清空后「home
    # 在工作区外」才真的越界，回退逻辑才能被断言。
    monkeypatch.setattr(tool_node_module, "_DEFAULT_EXTRA_ROOTS", ())


def _node(workspace: Path) -> ToolNode:
    return ToolNode("t", action_type="clone_repo", cwd=str(workspace))


def test_home_cache_used_when_inside_workspace(
    tmp_path, monkeypatch, real_path_validation
) -> None:
    workspace = tmp_path / "ws"
    home = workspace / "home"
    home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    assert _node(workspace)._default_clone_root() == home / ".xenon" / "repos"


def test_cache_falls_back_into_workspace_when_home_is_outside(
    tmp_path, monkeypatch, real_path_validation
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    root = _node(workspace)._default_clone_root()

    assert root == workspace / ".xenon" / "repos"
    assert root.is_relative_to(workspace)
