"""Tool subprocesses must not inherit the host's stdin.

On Windows a child that inherits a stdin the parent is blocking on can hang
before it prints anything: a real task's ``pytest`` never started, the tool
"timed out", and the turn stalled.  These tests pin the fix at both ends — the
argv the ``test`` action builds, and the invariant across the agent-facing
spawn sites.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from xenon.nodes.tool_families.test_tools import TestToolsMixin

ROOT = Path(__file__).resolve().parents[1]
SPAWN_FILES = (
    "xenon/nodes/tool_node.py",
    "xenon/nodes/tool_families/test_tools.py",
    "xenon/nodes/tool_families/git_tools.py",
    "xenon/nodes/tool_families/github_tools.py",
    "xenon/nodes/tool_families/script_tools.py",
    "xenon/hooks/runner.py",
    "xenon/catalog/gateway.py",
    "xenon/catalog/worktree.py",
)


class _Tool(TestToolsMixin):
    def __init__(self, cwd: Path, *, action: str = "") -> None:
        self.cwd = str(cwd)
        self.security_enabled = False
        self.test_framework = "auto"
        self.test_paths: list[str] = []
        self.test_args: list[str] = []
        self.test_command = None
        self.timeout = 30
        self.action = action
        self.interpreter = sys.executable
        self._output: list[str] = []

    def _validate_path(self, *_args, **_kwargs) -> None:
        return None

    def _run_tests_error(self, kind: str, message: str, **extra):
        return {"error_kind": kind, "error": message, **extra}

    def _write_output(self, _context, text: str) -> None:
        self._output.append(text)


@pytest.fixture
def captured_run(monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    class _Done:
        returncode = 0
        stdout = "1 passed"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return _Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_a_command_field_becomes_a_pytest_argv(tmp_path, captured_run):
    """``{"command": "pytest test_calc.py"}`` must run that file, not everything."""

    tool = _Tool(tmp_path, action="pytest test_calc.py -q")

    tool._run_tests({})

    argv, kwargs = captured_run[-1]
    assert argv[1:] == ["-m", "pytest", "test_calc.py", "-q"]
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_python_dash_m_pytest_is_understood(tmp_path, captured_run):
    tool = _Tool(tmp_path, action="python -m pytest tests -x")

    tool._run_tests({})

    argv, kwargs = captured_run[-1]
    assert argv[1:] == ["-m", "pytest", "tests", "-x"]
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_a_non_pytest_command_is_run_as_given(tmp_path, captured_run):
    tool = _Tool(tmp_path, action="npm test")

    tool._run_tests({})

    argv, kwargs = captured_run[-1]
    assert argv == ["npm", "test"]
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_no_agent_tool_spawn_inherits_stdin():
    offenders: list[str] = []
    for name in SPAWN_FILES:
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr in {"run", "Popen"}
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            ):
                continue
            keywords = {keyword.arg for keyword in node.keywords}
            # ``input=`` gives the child its own pipe with the payload; it is
            # just as isolated as an explicit DEVNULL stdin.
            if "stdin" not in keywords and "input" not in keywords:
                offenders.append(f"{name}:{node.lineno}")
    assert not offenders, f"subprocess inherits the host stdin: {offenders}"
