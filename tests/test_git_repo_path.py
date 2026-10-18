"""P1-1：git 工具支持在工作区子目录里的仓库执行（repo_path）。"""

from __future__ import annotations

import subprocess

from xenon.engine.context import AgentContext
from xenon.nodes.tool_node import ToolNode


def _git(*args: str, cwd) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def test_repo_path_runs_git_inside_the_repo(tmp_path, real_path_validation) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "a.txt").write_text("x", encoding="utf-8")

    node = ToolNode(
        "t",
        action_type="git",
        git_command="status",
        cwd=str(tmp_path),
        repo_path=str(repo),
    )
    result = node.execute(AgentContext({}))

    assert result["success"] is True
    assert "a.txt" in result["stdout"]


def test_without_repo_path_git_runs_at_workspace_root(
    tmp_path, real_path_validation
) -> None:
    node = ToolNode("t", action_type="git", git_command="status", cwd=str(tmp_path))
    result = node.execute(AgentContext({}))

    # tmp_path is not a git repository; running at the workspace root must fail.
    assert result["success"] is False


def test_repo_path_survives_normalization() -> None:
    params = ToolNode.normalize_params(
        {"git_command": "status", "repo_path": "repo"}, action_type="git"
    )
    assert params["repo_path"] == "repo"
