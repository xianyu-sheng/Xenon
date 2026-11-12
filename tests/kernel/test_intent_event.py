"""The intent classification is recorded in the ledger (doc §13.3 #5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.catalog.catalog import ToolCatalog
from xenon.catalog.gateway import LegacyToolGateway
from xenon.catalog.specs import CORE_SPECS
from xenon.kernel.contracts import Budget, Finish, TaskSpec
from xenon.kernel.controller import VerificationController
from xenon.kernel.policies import PermissionPolicy
from xenon.kernel.runtime import LoopRuntime
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


class _Finish:
    def propose(self, view: Any) -> Any:
        return Finish("done")


def test_the_classification_is_written_at_intake(tmp_path: Path) -> None:
    policy = PermissionPolicy(workspace=str(tmp_path))
    ledger = Ledger.open(tmp_path / "run.jsonl", "t1")
    runtime = LoopRuntime(
        catalog=ToolCatalog(CORE_SPECS, "core"),
        gateway=LegacyToolGateway(specs=CORE_SPECS, workspace=tmp_path, policy=policy),
        controller=VerificationController(),
        ledger=ledger,
        policies=(policy,),
    )

    runtime.run(
        TaskSpec(
            task_id="t1",
            objective="write a hello.py and run it",
            workspace=str(tmp_path),
            budget=Budget(),
            intent="write_code",
            intent_source="llm",
        ),
        _Finish(),
    )

    intents = [
        event.payload for event in ledger.read() if event.kind is EventKind.INTENT_CLASSIFIED
    ]
    assert intents == [
        {"intent": "write_code", "source": "llm", "mode": "auto"}
    ]


def test_an_unclassified_turn_writes_no_event(tmp_path: Path) -> None:
    policy = PermissionPolicy(workspace=str(tmp_path))
    ledger = Ledger.open(tmp_path / "run.jsonl", "t2")
    runtime = LoopRuntime(
        catalog=ToolCatalog(CORE_SPECS, "core"),
        gateway=LegacyToolGateway(specs=CORE_SPECS, workspace=tmp_path, policy=policy),
        controller=VerificationController(),
        ledger=ledger,
        policies=(policy,),
    )

    runtime.run(
        TaskSpec(task_id="t2", objective="x", workspace=str(tmp_path), budget=Budget()),
        _Finish(),
    )

    assert not [
        event for event in ledger.read() if event.kind is EventKind.INTENT_CLASSIFIED
    ]
