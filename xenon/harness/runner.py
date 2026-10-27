"""Headless harness runner (#203).

This is the first production-shaped entry into the kernel: real workspace, real
ToolExecutor, durable ledger, permission policy and finalizer.  It intentionally
does not import the REPL or legacy engines; the REPL migration is a later step.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from xenon.catalog.catalog import build_catalog
from xenon.catalog.finalizer import GitFinalizer
from xenon.catalog.gateway import LegacyToolGateway
from xenon.catalog.specs import CORE_SPECS
from xenon.harness.plan import HarnessPlan
from xenon.harness.sink import JsonlSink
from xenon.kernel.contracts import Budget, TaskResult, TaskSpec
from xenon.kernel.controller import VerificationController
from xenon.kernel.policies import PermissionMode, PermissionPolicy
from xenon.kernel.runtime import LoopRuntime
from xenon.kernel.strategies import PlanFirst
from xenon.ledger.events import canonical_json
from xenon.ledger.store import Ledger


@dataclass(frozen=True, slots=True)
class HarnessRun:
    result: TaskResult
    task_id: str
    ledger_path: Path


def run_plan(
    plan: HarnessPlan,
    *,
    workspace: str | Path = ".",
    ledger_dir: str | Path | None = None,
    task_id: str | None = None,
    permission: PermissionMode = PermissionMode.AUTO_EDIT,
    finalize: bool = True,
    max_steps: int = 20,
    sink: JsonlSink | None = None,
) -> HarnessRun:
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ValueError(f"workspace is not a directory: {root}")
    identifier = task_id or f"plan-{uuid.uuid4().hex[:12]}"
    directory = (
        Path(ledger_dir).expanduser()
        if ledger_dir is not None
        else Path.home() / ".xenon" / "ledger"
    )
    directory.mkdir(parents=True, exist_ok=True)
    ledger_path = directory / f"{identifier}.jsonl"
    ledger = Ledger(ledger_path, identifier)

    policy = PermissionPolicy(mode=permission, workspace=str(root))
    gateway = LegacyToolGateway(
        specs=CORE_SPECS,
        workspace=root,
        policy=policy,
    )
    runtime = LoopRuntime(
        catalog=build_catalog("core"),
        gateway=gateway,
        controller=VerificationController(),
        ledger=ledger,
        policies=(policy,),
        finalize=None,
        finalizer=GitFinalizer(root) if finalize else None,
        sink=sink,
    )
    spec = TaskSpec(
        task_id=identifier,
        objective=plan.objective,
        workspace=str(root),
        budget=Budget(max_steps=max_steps),
    )
    result = runtime.run(spec, PlanFirst(lambda view: plan.proposal))
    report = ledger.verify()
    if not report.valid:
        raise RuntimeError(f"ledger verification failed: {report.reason}")
    return HarnessRun(result=result, task_id=identifier, ledger_path=ledger_path)


def result_json(result: TaskResult) -> str:
    return canonical_json(result)


def write_result(result: TaskResult, stream: TextIO) -> None:
    stream.write(result_json(result))
    stream.write("\n")
    stream.flush()


__all__ = [
    "HarnessRun",
    "JsonlSink",
    "result_json",
    "run_plan",
    "write_result",
]
