"""Real ToolExecutor smoke for the gateway (#195).

This is the bridge test: actual bash/run_tests/git handlers, actual workspace,
and typed receipts.  It is deterministic and uses no network.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from xenon.catalog.gateway import LegacyToolGateway
from xenon.catalog.specs import CORE_SPECS, LEGACY_SPECS
from xenon.kernel.contracts import TestRun, ToolCall, VcsCommit


@dataclass
class Services:
    workspace: str
    task_id: str = "t1"
    current_step_id: str | None = "s1"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.e2e
def test_real_bash_pytest_and_commit_become_facts(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_ok():\n    assert 1 == 1\n", encoding="utf-8"
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", ".")

    gateway = LegacyToolGateway(
        specs=CORE_SPECS + LEGACY_SPECS,
        workspace=tmp_path,
    )
    services = Services(workspace=str(tmp_path))

    bash = gateway.execute(
        ToolCall(
            spec_name="bash",
            params={"command": "python -m pytest tests/test_a.py -q"},
        ),
        services,
    )
    bash_runs = [item for item in bash.effects if isinstance(item, TestRun)]
    assert bash_runs and bash_runs[0].tests_run == 1
    assert bash_runs[0].passed == 1

    structured = gateway.execute(
        ToolCall(
            spec_name="run_tests",
            params={
                "test_paths": ["tests/test_a.py"],
                "test_framework": "pytest",
            },
        ),
        services,
    )
    structured_runs = [
        item for item in structured.effects if isinstance(item, TestRun)
    ]
    assert structured_runs and structured_runs[0].tests_run == 1
    assert structured_runs[0].passed == 1

    commit = gateway.execute(
        ToolCall(spec_name="bash", params={"command": "git commit -m test"}),
        services,
    )
    commits = [item for item in commit.effects if isinstance(item, VcsCommit)]
    assert commits and commits[0].sha
    assert "tests/test_a.py" in commits[0].files
