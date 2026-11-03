"""Headless harness runner (#203).

This is the first production-shaped entry into the kernel: real workspace, real
ToolExecutor, durable ledger, permission policy and finalizer.  It intentionally
does not import the REPL or legacy engines; the REPL migration is a later step.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO

from xenon.catalog.catalog import build_catalog
from xenon.catalog.finalizer import GitFinalizer
from xenon.catalog.gateway import LegacyToolGateway
from xenon.catalog.worktree import GitWorktreeProbe
from xenon.harness.lessons import record_recurrences
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
from xenon.kernel.strategies import LoopProfile
from xenon.ledger.events import EventKind, EventSource, canonical_json
from xenon.ledger.store import Ledger

# Steps are the last-resort brake, not the primary one: the deterministic loop
# detector stops a stuck run, and a stalled run is stopped by time.  A complex
# task legitimately needs many steps, and a small cap turns "hard task" into
# "partial result".  1024 is large enough never to be the reason a real task
# fails, and still finite so a runaway cannot go forever.  ``0`` means no limit.
DEFAULT_MAX_STEPS = 1024


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
    tool_runtime: Any = None,
    audit_provider: Any = None,
    session_grants: Sequence[Any] = (),
    deny_rules: Sequence[Any] = (),
    ask_rules: Sequence[Any] = (),
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
        # Session grants are the human's remembered decisions (#373).  They are
        # passed in as plain values so the kernel stays a function of its inputs;
        # the front-end owns the book and the ledger records each addition.
        grants=tuple(session_grants),
        deny_rules=tuple(deny_rules),
        ask_rules=tuple(ask_rules),
    )
    catalog = build_catalog(catalog_profile, extra_specs=extra_specs)
    gateway = LegacyToolGateway(
        specs=catalog.specs(),
        workspace=root,
        policy=policy,
        context=gateway_context,
        runtime=tool_runtime,
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
        # Even without a finalizer we always want the worktree observation:
        # "no stray changes" is a question the host can answer deterministically.
        worktree_probe=GitWorktreeProbe(root),
        audit_provider=audit_provider,
    )
    return root, ledger_path, ledger, runtime


def _bind_accounting(strategy: Strategy, runtime: Any) -> None:
    """Let the runtime own the token totals for the calls this strategy makes."""

    from xenon.harness.llm import bind_runtime_hooks

    bind_runtime_hooks(strategy, runtime)


def _context_window(strategy: Strategy) -> int:
    """The window the strategy's model actually has (0 when unknown)."""

    provider = getattr(strategy, "actor", None) or getattr(
        strategy, "planner", None
    )
    config = getattr(provider, "config", None)
    try:
        return int(getattr(config, "context_window", 0) or 0)
    except (TypeError, ValueError):
        return 0


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
    max_steps: int = DEFAULT_MAX_STEPS,
    constraints: Sequence[str] = (),
    sink: JsonlSink | None = None,
    approval: ApprovalProvider | None = None,
    allow_approval: bool = False,
    approval_ttl_seconds: float = 60.0,
    mode: TaskMode = TaskMode.AUTO,
    catalog_profile: str = "core",
    gateway_context: Any = None,
    extra_specs: Sequence[ToolSpec] = (),
    tool_runtime: Any = None,
    on_runtime: Callable[[Any], None] | None = None,
    accept_debt: bool = False,
    context_blocks: Sequence[tuple[str, str]] = (),
    audit_provider: Any = None,
    parent_task_id: str = "",
    batch_id: str = "",
    max_stall_seconds: float | None = 180.0,
    session_grants: Sequence[Any] = (),
    deny_rules: Sequence[Any] = (),
    ask_rules: Sequence[Any] = (),
    lesson_store: Any = None,
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
        tool_runtime=tool_runtime,
        audit_provider=audit_provider,
        session_grants=session_grants,
        deny_rules=deny_rules,
        ask_rules=ask_rules,
    )
    spec = TaskSpec(
        task_id=identifier,
        objective=objective,
        workspace=str(root),
        user_objective=user_objective,
        constraints=tuple(constraints),
        mode=mode,
        budget=Budget(
            max_steps=max_steps if max_steps > 0 else None,
            context_window=_context_window(strategy),
            max_stall_seconds=max_stall_seconds,
        ),
        accept_debt=accept_debt,
        context_blocks=tuple(context_blocks),
        parent_task_id=parent_task_id,
        batch_id=batch_id,
    )
    # Self-evolution slice 3 (docs/design/self-evolution.md): a bounded set of
    # active lessons rides as constraints — the model *must* see them in the
    # system prompt — and every applied lesson is recorded in this task's
    # ledger.  Empty store, or a store that cannot load, means no lesson noise.
    if lesson_store is not None:
        try:
            applied = lesson_store.match_for_task()
        except Exception:  # noqa: BLE001 — a broken store must not fail the task
            applied = ()
        if applied:
            spec = replace(
                spec,
                constraints=spec.constraints
                + tuple(
                    f"[lesson {lesson.id}] {lesson.action_text}"
                    for lesson in applied
                ),
            )
            for lesson in applied:
                ledger.append(
                    EventKind.LESSON_APPLIED,
                    EventSource.GATE,
                    {
                        "lesson_id": lesson.id,
                        "action": lesson.action_text,
                        "kind": lesson.kind,
                    },
                )
            if callable(lesson_store.record_application):
                for lesson in applied:
                    try:
                        lesson_store.record_application(lesson.id)
                    except Exception:  # noqa: BLE001 — counters are advisory
                        pass
    _bind_accounting(strategy, runtime)
    if on_runtime is not None:
        # A front-end needs the runtime to *write events* (steering), not to
        # mutate it: everything it can do goes through the ledger.
        on_runtime(runtime)
    result = runtime.run(spec, strategy)
    report = ledger.verify()
    if not report.valid:
        raise RuntimeError(f"ledger verification failed: {report.reason}")
    # Slice 3: count recurrences of applied lessons and retire the ones that
    # keep failing.  Best-effort: a broken counter must not fail the task.
    if lesson_store is not None:
        try:
            retired = record_recurrences(ledger, lesson_store)
        except Exception:  # noqa: BLE001
            retired = ()
        for lesson_id in retired:
            ledger.append(
                EventKind.LESSON_RETIRED,
                EventSource.GATE,
                {"lesson_id": lesson_id, "reason": "recurrence threshold reached"},
            )
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
    max_steps: int = DEFAULT_MAX_STEPS,
    constraints: Sequence[str] = (),
    sink: JsonlSink | None = None,
    approval: ApprovalProvider | None = None,
    allow_approval: bool = False,
    approval_ttl_seconds: float = 60.0,
    catalog_profile: str = "core",
    gateway_context: Any = None,
    extra_specs: Sequence[ToolSpec] = (),
    accept_debt: bool = False,
    context_blocks: Sequence[tuple[str, str]] = (),
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
        budget=Budget(
            max_steps=max_steps,
            context_window=_context_window(strategy),
        ),
        accept_debt=accept_debt,
        context_blocks=tuple(context_blocks),
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
    max_steps: int = DEFAULT_MAX_STEPS,
    sink: JsonlSink | None = None,
    model_config: Any = None,
) -> HarnessRun:
    """Execute an approved plan.

    With a ``model_config`` the strategy is the real loop, so the steps have an
    actor that can actually call tools.  Without one, the run has a planner but
    nobody to execute — every step would emit an empty thought and be marked
    violated, which is what ``xenon exec --plan`` used to do.
    """

    if not Path(workspace).is_dir():
        # Basic preconditions first: a missing workspace is not an "actor"
        # problem, and reporting it as one would be misleading.
        raise ValueError(f"workspace is not a directory: {workspace}")

    # Two legitimate shapes for a given plan:
    #   * every step is capability-bound -> deterministic follow-plan, no model
    #     per step (the runtime executes the bound tool itself);
    #   * some step is free-form -> the step actor has to decide what to do, so
    #     the profile must not follow the plan blindly and a model is required.
    free_steps = [
        step.id
        for step in plan.proposal.steps
        if getattr(step, "intent", None) is None
    ]
    if model_config is not None:
        from dataclasses import replace

        from xenon.harness.llm import build_llm_loop_strategy

        profile = LoopProfile(
            plan=True,
            step_scope=True,
            follow_plan=not free_steps,
        )
        strategy: Any = replace(
            build_llm_loop_strategy(model_config, profile),
            # The plan is *given*: the model's job is to execute its steps,
            # not to invent a different one.
            planner=lambda view: plan.proposal,
        )
    else:
        if free_steps:
            raise ValueError(
                "run_plan requires model_config when steps are not "
                f"capability-bound (free-form steps: {free_steps}); without "
                "an actor they cannot be executed"
            )
        from xenon.kernel.contracts import Finish
        from xenon.kernel.strategies import LoopStrategy

        strategy = LoopStrategy(
            LoopProfile(plan=True, step_scope=True, follow_plan=True),
            actor=lambda view: Finish("completed"),
            planner=lambda view: plan.proposal,
        )
    return run_task(
        plan.objective,
        strategy=strategy,
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
