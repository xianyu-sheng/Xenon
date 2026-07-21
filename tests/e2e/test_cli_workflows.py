"""Deterministic subprocess tests for the installed-style Xenon CLI."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON_EXECUTABLE = shutil.which("python") or sys.executable


def _run_cli(*args: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "OPENAI_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "GITHUB_TOKEN": "",
        "GH_TOKEN": "",
        "NO_COLOR": "1",
    })
    return subprocess.run(
        [PYTHON_EXECUTABLE, "-m", "xenon.main", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_cli_version(tmp_path):
    result = _run_cli("--version", home=tmp_path)

    assert result.returncode == 0
    assert result.stdout.strip().startswith("xenon ")


def test_cli_executes_offline_datetime_workflow(tmp_path):
    workflow = tmp_path / "offline.yaml"
    workflow.write_text(
        """\
version: "1.0"
start_node: current_time
nodes:
  - id: current_time
    type: tool
    action_type: datetime
    output_slot: current_time
""",
        encoding="utf-8",
    )

    result = _run_cli("run", str(workflow), home=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "current_time" in result.stdout
    assert "success" in result.stdout.lower()


def test_cli_dry_run_parses_repository_workflow(tmp_path):
    result = _run_cli(
        "run",
        str(PROJECT_ROOT / "config" / "simple_code_flow.yaml"),
        "--dry-run",
        home=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert "Dry-run" in result.stdout
