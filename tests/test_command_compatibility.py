"""#176: unsupported shell controls fail closed with a safe alternative."""

from __future__ import annotations

import pytest

from xenon.nodes.tool_node import SecurityError, ToolNode
from xenon.utils.git_command import plan_git
from xenon.utils.test_command import plan_pytest


def _validate(command: str) -> None:
    node = ToolNode.__new__(ToolNode)
    node.security_enabled = True
    node._validate_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "if (Test-Path app.py) { pytest -q }",
        "foreach ($f in Get-ChildItem *.py) { pytest $f }",
        "while ($true) { git status }",
    ],
)
def test_powershell_control_flow_is_actionable(command: str) -> None:
    with pytest.raises(SecurityError, match="run_script"):
        _validate(command)


def test_quoted_control_flow_text_is_not_blocked() -> None:
    _validate('echo "if (x) { y }"')
    _validate("git log --grep='foreach ($x)'")


def test_supported_safe_shapes_remain_shell_free(tmp_path) -> None:
    repo = tmp_path / "repo"
    assert plan_pytest(
        f"Set-Location '{repo}'; python -m pytest \"tests/test_a.py::test_one\" -q",
        tmp_path,
    ) is not None
    assert plan_pytest(
        "$env:PYTHONDONTWRITEBYTECODE = '1'; cd /d repo; pytest tests -q 2>&1 | Select-Object -First 20",
        tmp_path,
    ) is not None
    assert plan_git(
        f"cd /d {repo} && git add app.py && git commit -m 'feat: app'",
        tmp_path,
    ) is not None
