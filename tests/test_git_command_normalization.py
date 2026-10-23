"""Chained git commands must not depend on the host shell."""

from __future__ import annotations

import subprocess

from xenon.engine.context import AgentContext
from xenon.nodes.tool_node import ToolNode
from xenon.utils.git_command import plan_git


def test_git_chain_is_split_without_a_shell() -> None:
    plan = plan_git(
        'cd repo; git add calc.py; git commit -m "feat: add calc"',
        "D:/workspace",
    )

    assert plan is not None
    assert plan.invocations == [
        ["git", "add", "calc.py"],
        ["git", "commit", "-m", "feat: add calc"],
    ]


def test_bare_commit_after_git_add_is_treated_as_git_commit() -> None:
    plan = plan_git('git add report.py && commit -m "feat: report"', "D:/repo")

    assert plan is not None
    assert plan.invocations == [
        ["git", "add", "report.py"],
        ["git", "commit", "-m", "feat: report"],
    ]


def test_git_chain_with_other_program_is_not_rewritten() -> None:
    assert plan_git("git add -A; python -m pytest", "D:/repo") is None
    assert plan_git("git push origin main", "D:/repo") is None


def test_tool_executes_git_add_and_commit_without_shell(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=tmp_path, check=True
    )
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    result = ToolNode(
        "t",
        action_type="command",
        action='git add -A; git commit -m "feat: add a"',
        cwd=str(tmp_path),
    ).execute(AgentContext({}))

    assert result["success"] is True
    assert result["committed_paths"] == ["a.py"]
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "feat: add a" in log
