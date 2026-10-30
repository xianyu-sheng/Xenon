"""Headless harness runner (#203).

This is the first production-shaped entry into the kernel: real workspace, real
ToolExecutor, durable ledger, permission policy and finalizer.  It intentionally
does not import the REPL or legacy engines; the REPL migration is a later step.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence, TextIO

from xenon.catalog.catalog import build_catalog
from xenon.catalog.finalizer import GitFinalizer
from xenon.catalog.gateway import LegacyToolGateway
from xenon.harness.plan import HarnessPlan
from xenon.harness.sink import JsonlSink
from xenon.kernel.contracts import (
    Budget,
    PlanProposal,
    TaskMode,
    TaskResult,
    TaskSpec,
    ToolSpec,
)
from xenon.kernel.controller import VerificationController
from xenon.kernel.policies import PermissionMode, PermissionPolicy
from xenon.kernel.protocols import ApprovalProvider, Strategy
from xenon.kernel.runtime import LoopRuntime
from xenon.kernel.strategies import PlanFirst
from xenon.ledger.events import canonical_json
from xenon.ledger.store import Ledger


@dataclass(frozen=True, slots=True)
class HarnessRun:
    result: TaskResult
    task_id: str
    ledger_path: Path


def _runtime_bundle(
    *,
    workspace: str | Path,
    ledger_dir: str | Path | None,
    task_id: str,
    permission: PermissionMode,
    finalize: bool,
    sink: JsonlSink | None,
    approval: ApprovalProvider | None,
    allow_approval: bool,
    approval_ttl_seconds: float,
    catalog_profile: str,
    gateway_context: Any,
    extra_specs: Sequence[ToolSpec],
) -> tuple[Path, Path, Ledger, LoopRuntime]:
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ValueError(f"workspace is not a directory: {root}")
    directory = (
        Path(ledger_dir).expanduser()
        if ledger_dir is not None
        else Path.home() / ".xenon" / "ledger"
    )
    directory.mkdir(parents=True, exist_ok=True)
    ledger_path = directory / f"{task_id}.jsonl"
    ledger = Ledger(ledger_path, task_id)
    policy = PermissionPolicy(
        mode=permission,
        workspace=str(root),
        allow_approval=allow_approval,
        approval_ttl_seconds=approval_ttl_seconds,
    )
    catalog = build_catalog(catalog_profile, extra_specs=extra_specs)
    gateway = LegacyToolGateway(
        specs=catalog.specs(),
        workspace=root,
        policy=policy,
        context=gateway_context,
    )
    runtime = LoopRuntime(
        catalog=catalog,
        gateway=gateway,
        controller=VerificationController(),
        ledger=ledger,
        policies=(policy,),
        finalize=None,
        finalizer=GitFinalizer(root) if finalize else None,
        sink=sink,
        approval=approval,
    )
    return root, ledger_path, ledger, runtime


def run_task(
    objective: str,
    *,
    user_objective: str = "",
    strategy: Strategy,
    workspace: str | Path = ".",
    ledger_dir: str | Path | None = None,
    task_id: str | None = None,
    permission: PermissionMode = PermissionMode.AUTO_EDIT,
    finalize: bool = True,
    max_steps: int = 20,
    constraints: Sequence[str] = (),
    sink: JsonlSink | None = None,
    approval: ApprovalProvider | None = None,
    allow_approval: bool = False,
    approval_ttl_seconds: float = 60.0,
    mode: TaskMode = TaskMode.AUTO,
    catalog_profile: str = "core",
    gateway_context: Any = None,
    extra_specs: Sequence[ToolSpec] = (),
) -> HarnessRun:
    """Run one objective through the kernel with an injected strategy.

    Both the deterministic plan path and the LLM path share this function, so
    ledger placement, policy, finalization and error handling cannot drift.
    """

    identifier = task_id or f"task-{uuid.uuid4().hex[:12]}"
    root, ledger_path, ledger, runtime = _runtime_bundle(
        workspace=workspace,
        ledger_dir=ledger_dir,
        task_id=identifier,
        permission=permission,
        finalize=finalize,
        sink=sink,
        approval=approval,
        allow_approval=allow_approval,
        approval_ttl_seconds=approval_ttl_seconds,
        catalog_profile=catalog_profile,
        gateway_context=gateway_context,
        extra_specs=extra_specs,
    )
    spec = TaskSpec(
        task_id=identifier,
        objective=objective,
        workspace=str(root),
        user_objective=user_objective,
        constraints=tuple(constraints),
        mode=mode,
        budget=Budget(max_steps=max_steps),
    )
    result = runtime.run(spec, strategy)
    report = ledger.verify()
    if not report.valid:
        raise RuntimeError(f"ledger verification failed: {report.reason}")
    return HarnessRun(result=result, task_id=identifier, ledger_path=ledger_path)


def resume_task(
    *,
    task_id: str,
    plan: PlanProposal,
    plan_digest: str,
    approval_id: str,
    strategy: Strategy,
    objective: str = "",
    user_objective: str = "",
    workspace: str | Path = ".",
    ledger_dir: str | Path | None = None,
    permission: PermissionMode = PermissionMode.AUTO_EDIT,
    finalize: bool = True,
    max_steps: int = 20,
    constraints: Sequence[str] = (),
    sink: JsonlSink | None = None,
    approval: ApprovalProvider | None = None,
    allow_approval: bool = False,
    approval_ttl_seconds: float = 60.0,
    catalog_profile: str = "core",
    gateway_context: Any = None,
    extra_specs: Sequence[ToolSpec] = (),
) -> HarnessRun:
    """Resume an approved plan in its existing ledger.

    This is the headless counterpart of the REPL approval gate.  It never
    creates a new task id or ledger: the runtime validates the digest and
    phase before appending the execution events.
    """

    root, ledger_path, ledger, runtime = _runtime_bundle(
        workspace=workspace,
        ledger_dir=ledger_dir,
        task_id=task_id,
        permission=permission,
        finalize=finalize,
        sink=sink,
        approval=approval,
        allow_approval=allow_approval,
        approval_ttl_seconds=approval_ttl_seconds,
        catalog_profile=catalog_profile,
        gateway_context=gateway_context,
        extra_specs=extra_specs,
    )
    spec = TaskSpec(
        task_id=task_id,
        objective=objective,
        workspace=str(root),
        user_objective=user_objective,
        constraints=tuple(constraints),
        mode=TaskMode.TASK,
        budget=Budget(max_steps=max_steps),
    )
    result = runtime.resume_planned(
        spec,
        strategy,
        plan,
        plan_digest=plan_digest,
        approval_id=approval_id,
    )
    report = ledger.verify()
    if not report.valid:
        raise RuntimeError(f"ledger verification failed: {report.reason}")
    return HarnessRun(result=result, task_id=task_id, ledger_path=ledger_path)


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
    return run_task(
        plan.objective,
        strategy=PlanFirst(lambda view: plan.proposal),
        workspace=workspace,
        ledger_dir=ledger_dir,
        task_id=task_id,
        permission=permission,
        finalize=finalize,
        max_steps=max_steps,
        sink=sink,
    )


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
    "resume_task",
    "run_plan",
    "run_task",
    "write_result",
]
