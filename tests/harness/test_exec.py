"""Headless harness vertical slice (#203)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from xenon.harness.plan import parse_plan
from xenon.harness.runner import run_plan
from xenon.kernel.policies import PermissionMode
from xenon.ledger.store import Ledger


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )


def _repo(tmp_path: Path) -> None:
    (tmp_path / "calc").mkdir()
    (tmp_path / "tests").mkdir()
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")


def _plan_dict() -> dict:
    add_source = chr(10).join(
        ["def add(a, b):", "    return a + b"]
    ) + chr(10)
    test_source = chr(10).join(
        [
            "from calc.add import add",
            "",
            "def test_add():",
            "    assert add(1, 2) == 3",
        ]
    ) + chr(10)
    return {
        "objective": "add a function and test it",
        "analysis": "edit, verify, finalize",
        "steps": [
            {
                "id": 1,
                "kind": "edit",
                "capability": "edit",
                "params": {
                    "files": [
                        {"path": "calc/add.py", "content": add_source},
                        {"path": "tests/test_add.py", "content": test_source},
                    ]
                },
                "expects": [
                    {
                        "id": "edit-1",
                        "kind": "file_changed",
                        "target": "calc/add.py",
                    }
                ],
            },
            {
                "id": 2,
                "kind": "verify",
                "capability": "verify",
                "params": {"command": "python -m pytest tests/test_add.py -q"},
                "depends_on": [1],
                "expects": [
                    {
                        "id": "test-1",
                        "kind": "test_passed",
                        "target": "tests/test_add.py",
                    }
                ],
            },
        ],
    }


def test_run_plan_uses_real_workspace_ledger_and_finalizer(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    plan = parse_plan(_plan_dict())

    run = run_plan(
        plan,
        workspace=tmp_path,
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-ledger",
        task_id="harness-1",
        permission=PermissionMode.AUTO_EDIT,
    )

    assert run.result.accepted is True
    assert run.result.finalize_sha
    assert run.ledger_path.exists()
    assert Ledger(run.ledger_path, "harness-1").verify().valid
    assert _git(tmp_path, "status", "--porcelain").stdout.strip() == ""
    assert "xenon:" in _git(tmp_path, "log", "-1", "--pretty=%s").stdout


def test_xenon_exec_emits_jsonl_and_result(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan_dict()), encoding="utf-8")
    output = tmp_path / "result.json"
    ledger_dir = tmp_path / "ledger"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "xenon",
            "exec",
            "--plan",
            str(plan_path),
            "--workspace",
            str(repo),
            "--ledger-dir",
            str(ledger_dir),
            "--task-id",
            "cli-task",
            "--jsonl",
            "--output",
            str(output),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines
    events = [json.loads(line) for line in lines]
    assert all("seq" in event and "kind" in event for event in events)
    assert output.exists()
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["accepted"] is True
    assert (ledger_dir / "cli-task.jsonl").exists()
