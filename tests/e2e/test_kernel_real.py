"""End-to-end kernel smoke: real catalog, gateway, ledger and controller (#195/#196)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from xenon.catalog.catalog import ToolCatalog
from xenon.catalog.gateway import LegacyToolGateway
from xenon.catalog.specs import CORE_SPECS
from xenon.kernel.contracts import (
    Budget,
    CallTool,
    Capability,
    Finish,
    TaskSpec,
    ToolIntent,
)
from xenon.kernel.controller import VerificationController
from xenon.kernel.policies import PermissionPolicy
from xenon.kernel.runtime import LoopRuntime
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


@dataclass
class Services:
    workspace: str
    task_id: str = "t1"
    current_step_id: str | None = None

    @property
    def is_cancelled(self) -> bool:
        return False


class Script:
    def __init__(self, actions: list[Any]) -> None:
        self._actions = list(actions)

    def propose(self, view: Any) -> Any:
        if not self._actions:
            return Finish("unexpected extra step")
        return self._actions.pop(0)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.e2e
def test_real_calc_task_is_delivered_with_facts(tmp_path: Path) -> None:
    (tmp_path / "calc").mkdir()
    (tmp_path / "tests").mkdir()
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")

    ledger = Ledger.in_memory("t1")
    runtime = LoopRuntime(
        catalog=ToolCatalog(CORE_SPECS, "core"),
        gateway=LegacyToolGateway(
            specs=CORE_SPECS,
            workspace=tmp_path,
            policy=PermissionPolicy(workspace=str(tmp_path)),
        ),
        controller=VerificationController(),
        ledger=ledger,
        policies=(PermissionPolicy(workspace=str(tmp_path)),),
    )
    spec = TaskSpec(
        task_id="t1",
        objective="add a function and test it",
        workspace=str(tmp_path),
        budget=Budget(max_steps=10),
    )
    script = Script(
        [
            CallTool(
                ToolIntent(
                    capability=Capability.EDIT,
                    params={
                        "files": [
                            {
                                "path": "calc/add.py",
                                "content": "def add(a, b):\n    return a + b\n",
                            },
                            {
                                "path": "tests/test_add.py",
                                "content": (
                                    "from calc.add import add\n\n"
                                    "def test_add():\n"
                                    "    assert add(1, 2) == 3\n"
                                ),
                            },
                        ]
                    },
                )
            ),
            CallTool(
                ToolIntent(
                    capability=Capability.SHELL,
                    params={"command": "git add ."},
                )
            ),
            CallTool(
                ToolIntent(
                    capability=Capability.VERIFY,
                    params={"command": "python -m pytest tests/test_add.py -q"},
                )
            ),
            CallTool(
                ToolIntent(
                    capability=Capability.VCS,
                    params={"command": "git commit -m add"},
                )
            ),
            Finish("done"),
        ]
    )

    result = runtime.run(spec, script)

    assert result.accepted is True
    assert result.finalize_sha
    assert result.evidence_status.value == "verified"
    assert (tmp_path / "calc" / "add.py").exists()
    receipts = ledger.read(EventKind.TOOL_RECEIPT)
    assert receipts
    assert ledger.verify().valid
