"""Structured run_tests adapter: no shell, real exit codes, project interpreter."""

from __future__ import annotations

import subprocess
from pathlib import Path

from xenon.engine.context import AgentContext
from xenon.nodes.tool_node import ToolNode


def _run(tmp_path: Path, **kwargs):
    return ToolNode(
        "run-tests",
        action_type="run_tests",
        cwd=str(tmp_path),
        **kwargs,
    ).execute(AgentContext({}))


def test_pytest_success_returns_structured_result(tmp_path: Path) -> None:
    (tmp_path / "test_ok.py").write_text(
        "def test_ok():\n    assert 2 + 3 == 5\n", encoding="utf-8"
    )

    result = _run(
        tmp_path,
        test_framework="pytest",
        test_paths=["test_ok.py"],
        test_args=["-q"],
    )

    assert result["success"] is True
    assert result["returncode"] == 0
    assert result["exit_code"] == 0
    assert result["framework"] == "pytest"
    assert result["duration"] >= 0
    assert "1 passed" in result["stdout"]
    assert result["command"].endswith("pytest test_ok.py -q")
    assert result["interpreter"]


def test_pytest_failure_preserves_real_exit_code(tmp_path: Path) -> None:
    (tmp_path / "test_bad.py").write_text(
        "def test_bad():\n    assert 1 == 2\n", encoding="utf-8"
    )

    result = _run(
        tmp_path,
        test_framework="pytest",
        test_paths=["test_bad.py"],
        test_args=["-q"],
    )

    assert result["success"] is False
    assert result["returncode"] == 1
    assert result["error_kind"] == "test_failure"
    assert "failed" in (result["stdout"] + result["stderr"]).lower()


def test_missing_test_path_fails_before_spawning(tmp_path: Path, monkeypatch) -> None:
    calls: list[object] = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("must not spawn for a missing path")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _run(tmp_path, test_paths=["tests/does_not_exist.py"])

    assert result["success"] is False
    assert result["error_kind"] == "missing_test_path"
    assert calls == []


def test_timeout_is_structured(tmp_path: Path) -> None:
    (tmp_path / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(5)\n", encoding="utf-8"
    )

    result = _run(
        tmp_path,
        test_paths=["test_slow.py"],
        timeout=0.2,
    )

    assert result["success"] is False
    assert result["returncode"] == -1
    assert result["error_kind"] == "timeout"
    assert result["duration"] >= 0


def test_unittest_runs_without_shell(tmp_path: Path) -> None:
    (tmp_path / "test_unit.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n"
        "    def test_ok(self):\n        self.assertEqual(2 + 3, 5)\n",
        encoding="utf-8",
    )

    result = _run(
        tmp_path,
        test_framework="unittest",
        test_paths=["test_unit"],
    )

    assert result["success"] is True
    assert result["returncode"] == 0
    assert result["framework"] == "unittest"


def test_custom_command_is_an_argv_vector_not_a_shell(tmp_path: Path) -> None:
    (tmp_path / "check.py").write_text("print('custom-ok')\n", encoding="utf-8")

    result = _run(
        tmp_path,
        test_framework="custom",
        test_command=["python", "check.py"],
    )

    assert result["success"] is True
    assert result["returncode"] == 0
    assert result["argv"][0] == "python"
    assert result["argv"][-1] == "check.py"


def test_run_tests_never_uses_shell(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    seen: list[dict] = []
    original = subprocess.run

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    result = _run(tmp_path, test_paths=["test_ok.py"])

    assert result["success"] is True
    assert seen and all(kwargs.get("shell") is False for kwargs in seen)


def test_project_interpreter_is_preferred(tmp_path: Path, monkeypatch) -> None:
    from xenon.nodes.tool_families import test_tools

    scripts = tmp_path / ".venv" / ("Scripts" if test_tools.sys.platform == "win32" else "bin")
    scripts.mkdir(parents=True)
    executable = scripts / ("python.exe" if test_tools.sys.platform == "win32" else "python")
    executable.write_text("", encoding="utf-8")

    resolved, source = test_tools._resolve_interpreter(tmp_path, "")
    assert resolved == executable
    assert source == "project"


def test_explicit_missing_interpreter_is_actionable(tmp_path: Path) -> None:
    result = _run(tmp_path, interpreter="missing-python.exe")

    assert result["success"] is False
    assert result["error_kind"] == "missing_interpreter"
    assert "missing-python.exe" in result["error"]


def test_task_state_records_run_tests_as_verification() -> None:
    from xenon.engine.task_state import TaskState

    state = TaskState(
        objective="x", intent="write_code", execution_level=3, required=["verify"]
    )
    state.record_tool(
        "run_tests",
        {"test_framework": "pytest", "test_paths": ["tests/test_x.py"]},
        success=True,
        result="3 passed",
        metadata={"returncode": 0},
    )

    assert state.tests
    assert state.failed_tests == []
    assert "run_tests" in state.tests[0]


def test_run_tests_feeds_real_test_artifact_and_commit_into_task_state(
    tmp_path: Path,
) -> None:
    from xenon.engine.task_state import TaskState

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "mathx.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "test_mathx.py").write_text(
        "from mathx import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
    (repo / "test_mathx.py").write_text(
        "from mathx import add" + chr(10) + chr(10) + "def test_add():" + chr(10) + "    assert add(2, 3) == 5" + chr(10) + chr(10) + "# regression marker" + chr(10),
        encoding="utf-8",
    )

    result = ToolNode(
        "run-tests",
        action_type="run_tests",
        cwd=str(repo),
        test_paths=["test_mathx.py"],
    ).execute(AgentContext({}))
    assert result["success"] is True

    subprocess.run(["git", "add", "test_mathx.py"], cwd=repo, check=True)
    commit = subprocess.run(
        ["git", "commit", "-m", "test: add regression"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    # The commit ran successfully in the fixture; feed structured facts to the
    # same TaskState API used by ToolExecutionTracker.
    state = TaskState(
        objective="add a test",
        intent="write_code",
        execution_level=3,
        required=["verify"],
    )
    state.record_tool(
        "write_file",
        {"file_path": "test_mathx.py"},
        success=True,
        metadata={
            "changed_paths": ["test_mathx.py"],
            "content_chars": (repo / "test_mathx.py").stat().st_size,
        },
    )
    state.record_tool(
        "run_tests",
        {"test_framework": "pytest", "test_paths": ["test_mathx.py"]},
        success=True,
        result=result["stdout"],
        metadata={"returncode": 0, "stdout": result["stdout"]},
    )
    state.record_tool(
        "git",
        {"git_command": "commit"},
        success=True,
        result=commit.stdout,
        metadata={"committed_paths": ["test_mathx.py"]},
    )

    assert state.tests
    assert state.test_artifacts == ["test_mathx.py"]
    assert state.commit


def test_test_path_outside_workspace_is_rejected(
    tmp_path: Path, real_path_validation
) -> None:
    outside = tmp_path / "outside_test.py"
    outside.write_text("def test_ok():" + chr(10) + "    assert True" + chr(10), encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _run(workspace, test_paths=["../outside_test.py"])

    assert result["success"] is False
    assert result["error_kind"] == "security_error"
    assert "越界" in result["error"]
