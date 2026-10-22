"""Pytest command normalisation must not depend on the host shell."""

from __future__ import annotations

from pathlib import Path

from xenon.utils.test_command import plan_pytest


def test_plain_pytest_command_becomes_argument_vector() -> None:
    plan = plan_pytest("python -m pytest tests/test_app.py -q", "D:/repo")

    assert plan is not None
    assert plan.cwd == str(Path("D:/repo"))
    assert plan.invocations == [["tests/test_app.py", "-q"]]


def test_cd_and_powershell_pipeline_are_removed(tmp_path) -> None:
    repo = tmp_path / "repo"
    plan = plan_pytest(
        f"cd {repo}; python -m pytest tests -q -k coverage "
        "2>&1 | Select-Object -First 40",
        tmp_path / "workspace",
    )

    assert plan is not None
    assert plan.cwd == str(repo)
    assert plan.invocations == [["tests", "-q", "-k", "coverage"]]


def test_powershell_environment_assignment_is_preserved() -> None:
    plan = plan_pytest(
        '$env:PYTHONDONTWRITEBYTECODE = "1"; cd repo; '
        "pytest tests/test_app.py -q",
        "D:/workspace",
    )

    assert plan is not None
    assert plan.cwd == str(Path("D:/workspace/repo"))
    assert plan.invocations == [["tests/test_app.py", "-q"]]
    assert plan.env == {"PYTHONDONTWRITEBYTECODE": "1"}


def test_multiple_pytest_commands_are_preserved() -> None:
    plan = plan_pytest(
        "cd repo; python -m pytest tests/test_a.py -q; "
        "python -m pytest tests -q -k coverage",
        "D:/workspace",
    )

    assert plan is not None
    assert plan.invocations == [
        ["tests/test_a.py", "-q"],
        ["tests", "-q", "-k", "coverage"],
    ]


def test_non_pytest_command_is_not_rewritten() -> None:
    assert plan_pytest("npm test", "D:/repo") is None
    assert plan_pytest("python -m pytest tests; git status", "D:/repo") is None


def test_command_tool_runs_pytest_without_shell(tmp_path) -> None:
    """The shell-free branch must return the real pytest exit code/output."""
    from xenon.engine.context import AgentContext
    from xenon.nodes.tool_node import ToolNode

    (tmp_path / "test_ok.py").write_text(
        "def test_ok():\n    assert 2 + 3 == 5\n", encoding="utf-8"
    )
    result = ToolNode(
        "t",
        action_type="command",
        action="python -m pytest test_ok.py -q",
        cwd=str(tmp_path),
    ).execute(AgentContext({}))

    assert result["success"] is True
    assert result["returncode"] == 0
    assert "1 passed" in result["stdout"]


def test_quoted_node_id_and_k_expression_lose_shell_quotes() -> None:
    plan = plan_pytest(
        'python -m pytest "tests/test_a.py::TestA::test_one" '
        '-k "one or two" -q',
        "D:/repo",
    )

    assert plan is not None
    assert plan.invocations == [
        ["tests/test_a.py::TestA::test_one", "-k", "one or two", "-q"]
    ]
