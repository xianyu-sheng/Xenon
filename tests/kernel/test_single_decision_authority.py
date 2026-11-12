"""Single decision authority (defect 2): kernel rulings are final.

The kernel's ``PermissionPolicy`` decides with grants, rules and mode — more
information than the legacy executor's turn-level check.  These tests pin the
exact conflict that existed: a shell capability the kernel had *granted* was
re-decided and denied by the executor's ``execution_policy_denial`` whenever
the turn level was below EXECUTE, and the user saw a second escalation
prompt for a permission they had already given.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.catalog.catalog import ToolCatalog
from xenon.catalog.gateway import LegacyToolGateway
from xenon.catalog.specs import CORE_SPECS
from xenon.engine.context import AgentContext
from xenon.kernel.contracts import (
    Budget,
    CallTool,
    Capability,
    Finish,
    TaskSpec,
    ToolIntent,
)
from xenon.kernel.controller import VerificationController
from xenon.kernel.policies import PermissionMode, PermissionPolicy, ScopedGrant
from xenon.kernel.runtime import LoopRuntime
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


class Script:
    def __init__(self, actions: list[Any]) -> None:
        self._actions = list(actions)

    def propose(self, view: Any) -> Any:
        return self._actions.pop(0) if self._actions else Finish("done")


def test_a_granted_shell_runs_even_below_execute_level(tmp_path: Path) -> None:
    """The kernel granted shell (``allow`` in an earlier EXECUTE turn); a
    WRITE-level turn reuses it.  The executor must not re-decide."""

    context = AgentContext({"_execution_level": 2})  # WRITE, not EXECUTE
    policy = PermissionPolicy(
        mode=PermissionMode.AUTO_EDIT,
        workspace=str(tmp_path),
        grants=(ScopedGrant(capability=Capability.SHELL, target="*"),),
        allow_approval=False,
    )
    ledger = Ledger.in_memory("t1")
    runtime = LoopRuntime(
        catalog=ToolCatalog(CORE_SPECS, "core"),
        gateway=LegacyToolGateway(
            specs=CORE_SPECS,
            workspace=tmp_path,
            policy=policy,
            context=context,
            catalog=ToolCatalog(CORE_SPECS, "core"),
        ),
        controller=VerificationController(),
        ledger=ledger,
        policies=(policy,),
    )
    command = f"{Path(sys.executable).name} --version"
    result = runtime.run(
        TaskSpec(
            task_id="t1",
            objective="report the python version",
            workspace=str(tmp_path),
            budget=Budget(max_steps=5),
        ),
        Script(
            [
                CallTool(
                    ToolIntent(
                        capability=Capability.SHELL,
                        params={"command": command},
                    )
                ),
                Finish("done"),
            ]
        ),
    )

    receipts = [
        event.payload["receipt"]
        for event in ledger.read()
        if event.kind is EventKind.TOOL_RECEIPT
    ]
    shell = [item for item in receipts if item["tool"] == "bash"]
    assert shell, "the granted shell call must reach a receipt"
    assert shell[0]["status"] == "ok", shell[0].get("error")
    assert "本轮执行策略" not in (shell[0].get("error") or "")
    assert result.phase.value in {"finalized", "blocked"}
