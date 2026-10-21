"""Git tool argument handling for real plan-execute runs."""

from __future__ import annotations

import subprocess

from xenon.engine.context import AgentContext
from xenon.nodes.tool_node import ToolNode


def _run_git(repo, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path):
    _run_git(tmp_path, "init", "-q")
    _run_git(tmp_path, "config", "user.email", "test@example.com")
    _run_git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    return tmp_path


def test_git_add_accepts_multiple_targets(tmp_path):
    repo = _repo(tmp_path)
    node = ToolNode("g", action_type="git", cwd=str(repo))

    result = node._git(AgentContext())

    # The default status call must remain a status call.
    assert result["success"] is True

    node = ToolNode(
        "g2",
        action_type="git",
        git_command="add a.txt b.txt",
        cwd=str(repo),
    )
    result = node._git(AgentContext())

    assert result["success"] is True
    assert "A  a.txt" in _run_git(repo, "status", "--short")
    assert "A  b.txt" in _run_git(repo, "status", "--short")


def test_git_action_list_is_normalized(tmp_path):
    repo = _repo(tmp_path)
    node = ToolNode(
        "g",
        action_type="git",
        action=["add", "a.txt", "b.txt"],
        cwd=str(repo),
    )

    result = node._git(AgentContext())

    assert result["success"] is True
    assert "A  a.txt" in _run_git(repo, "status", "--short")


def test_git_commit_message_is_not_literal_m_flag(tmp_path):
    repo = _repo(tmp_path)
    _run_git(repo, "add", "a.txt")
    node = ToolNode(
        "g",
        action_type="git",
        git_command='commit -m "feat: real subject"',
        cwd=str(repo),
    )

    result = node._git(AgentContext())

    assert result["success"] is True
    assert _run_git(repo, "log", "-1", "--format=%s") == "feat: real subject"
    assert "Co-authored-by: Xenon" in _run_git(repo, "log", "-1", "--format=%B")
