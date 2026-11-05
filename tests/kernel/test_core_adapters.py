"""The core surface exposes structured test and git adapters (#423).

A bash ``pytest`` call and a bash ``git status`` both produce opaque receipts.
The task audit needs a real ``TestRun`` fact and the model needs a low-friction
read of repository state, so both adapters are explicit core tools.
"""

from __future__ import annotations

from pathlib import Path

from xenon.harness.runner import run_task
from xenon.kernel.contracts import CallTool, Capability, Finish, ToolIntent
from xenon.ledger.store import Ledger


class _RunOneTest:
    def __init__(self) -> None:
        self.phase = 0

    def propose(self, view):  # noqa: ANN001 - protocol shape
        if self.phase == 0:
            self.phase = 1
            return CallTool(
                ToolIntent(
                    capability=Capability.VERIFY,
                    params={"test_paths": ["tests/test_ok.py"]},
                )
            )
        if self.phase == 1:
            self.phase = 2
            return CallTool(
                ToolIntent(
                    capability=Capability.VCS,
                    params={"git_command": "status"},
                )
            )
        return Finish("done")


def test_test_and_git_bind_without_a_shell_call(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    run = run_task(
        "run the test and read git status",
        strategy=_RunOneTest(),
        workspace=tmp_path,
        ledger_dir=tmp_path / "ledger",
        finalize=False,
        max_steps=0,
    )

    receipts = [
        event.payload["receipt"]
        for event in Ledger.open(run.ledger_path).read()
        if event.kind.value == "tool_receipt"
    ]
    by_tool = {item["tool"]: item for item in receipts}

    assert "test" in by_tool, receipts
    test_receipt = by_tool["test"]
    assert test_receipt["status"] == "ok", test_receipt.get("error")
    effects = [item for item in test_receipt["effects"] if item["type"] == "test_run"]
    assert effects and effects[0]["passed"] == 1
    assert effects[0]["exit_code"] == 0

    assert by_tool["git"]["status"] == "ok"
