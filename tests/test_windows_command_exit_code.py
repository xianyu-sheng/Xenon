"""P0-1 回归：Windows 命令的退出码不能被 `2>&1 | ...` 伪装成失败。

真实事故（2026-10-17）：`git push ... 2>&1 | Select-Object -Last 10` 实际成功，
但 PowerShell 把 git 的 stderr 进度转成 error record，`powershell -Command`
返回 1 → Xenon 报失败 → 模型反复重试 push，浪费约 5 分钟。
"""

from __future__ import annotations

import sys

import pytest

from xenon.engine.context import AgentContext
from xenon.nodes.tool_node import ToolNode


def _run(command: str, cwd) -> dict:
    return ToolNode(
        "t", action_type="command", action=command, cwd=str(cwd)
    ).execute(AgentContext({}))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell semantics")
def test_stderr_output_success_is_not_reported_as_failure(tmp_path) -> None:
    script = tmp_path / "emit_stderr.py"
    script.write_text(
        "import sys; print('to-stderr', file=sys.stderr)\n", encoding="utf-8"
    )
    result = _run(
        f'python "{script}" 2>&1 | Select-Object -Last 1', tmp_path
    )
    assert result["success"] is True
    assert result["returncode"] == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell semantics")
def test_real_nonzero_exit_is_still_reported(tmp_path) -> None:
    script = tmp_path / "exit_three.py"
    script.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
    result = _run(f'python "{script}"', tmp_path)
    assert result["success"] is False
    assert result["returncode"] == 3


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell semantics")
def test_pure_cmdlet_success_still_reports_zero(tmp_path) -> None:
    result = _run("Write-Output hello", tmp_path)
    assert result["success"] is True
    assert result["returncode"] == 0
