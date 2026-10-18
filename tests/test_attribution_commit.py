"""强制署名：真实 git 仓库里 commit 的 Co-authored-by 验证。"""

from __future__ import annotations

import subprocess

from xenon.engine.context import AgentContext
from xenon.nodes.tool_node import ToolNode


def _init_repo(path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "tester@example.com"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tester"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


def _last_message(path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=str(path),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_command_tool_commit_carries_trailer(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("XENON_ATTRIBUTION", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "a.txt").write_text("x", encoding="utf-8")

    result = ToolNode(
        "t",
        action_type="command",
        action='git add a.txt; git commit -m "feat: add a"',
        cwd=str(repo),
    ).execute(AgentContext({}))
    assert result["success"] is True
    assert "Co-authored-by: Xenon" in _last_message(repo)


def test_git_tool_commit_carries_trailer(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("XENON_ATTRIBUTION", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "a.txt").write_text("x", encoding="utf-8")

    add = ToolNode(
        "t",
        action_type="command",
        action="git add a.txt",
        cwd=str(repo),
    ).execute(AgentContext({}))
    assert add["success"] is True

    result = ToolNode(
        "t",
        action_type="git",
        git_command="commit",
        action="-m msg",
        cwd=str(repo),
    ).execute(AgentContext({}))
    assert result["success"] is True
    assert "Co-authored-by: Xenon" in _last_message(repo)


def test_disabled_attribution_commits_without_trailer(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XENON_ATTRIBUTION", "0")
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "a.txt").write_text("x", encoding="utf-8")

    result = ToolNode(
        "t",
        action_type="command",
        action='git add a.txt; git commit -m "chore: no attribution"',
        cwd=str(repo),
    ).execute(AgentContext({}))
    assert result["success"] is True
    assert "Co-authored-by: Xenon" not in _last_message(repo)
