"""Child runtimes: one loop, one ledger per child (#204, minimal slice).

A child task is not a second execution model.  It is the same ``run_task`` loop
with its own ledger, its own objective and no delivery commit — so a parent can
delegate work without sharing mutable task state.

What this slice deliberately does **not** do: kernel-level spawning (a `spawn`
capability), streaming the child's events into the parent ledger, or nested
children.  Depth is structurally 1: the child runs the same loop, and the loop has
no spawn tool.
"""

from __future__ import annotations

import concurrent.futures
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from xenon.harness.llm import ModelConfig, build_llm_loop_strategy
from xenon.harness.runner import run_task
from xenon.kernel.policies import PermissionMode
from xenon.kernel.strategies import LoopProfile

# Child profiles are a subset of the mode catalog: a child works on one
# delegated objective, so it never enables plan approval or review loops.
CHILD_PROFILES: dict[str, LoopProfile] = {
    "react": LoopProfile(),
    "plan-execute": LoopProfile(plan=True, step_scope=True, follow_plan=True),
    "plan-react": LoopProfile(plan=True, step_scope=True, step_calls=6),
    "kernel": LoopProfile(plan=True, step_scope=True, step_calls=6),
    "reflection": LoopProfile(reflect_rounds=1, reflect_calls=1),
}

MAX_CHILD_DEPTH = 1


@dataclass(frozen=True, slots=True)
class ChildOutcome:
    """One child run, as the parent records it."""

    child_id: str
    objective: str
    profile: str
    accepted: bool
    phase: str
    answer: str
    ledger_path: str
    debt: tuple[str, ...]
    tool_calls: int
    elapsed_seconds: float
    error: str | None = None

    @property
    def delivered(self) -> bool:
        """A child delivered only if the kernel said so."""

        return self.accepted and self.phase == "finalized" and self.error is None

    def summary(self, *, limit: int = 400) -> str:
        head = f"[{self.child_id}] {self.phase}"
        if self.error:
            return f"{head} · 失败: {self.error}"
        body = self.answer.strip()[:limit] or "（无最终回答）"
        tail = f" · debt: {'; '.join(self.debt)}" if self.debt else ""
        return f"{head}{tail}\n{body}\nledger: {self.ledger_path}"


def run_child_task(
    *,
    objective: str,
    workspace: str | Path,
    model: ModelConfig,
    profile: str = "kernel",
    parent_task_id: str = "",
    ledger_dir: str | Path | None = None,
    permission: PermissionMode = PermissionMode.AUTO_EDIT,
    max_steps: int = 20,
    timeout: float | None = None,
    client: Any = None,
    gateway_context: Any = None,
    index: int = 1,
) -> ChildOutcome:
    """Run one delegated task on the loop, in its own ledger."""

    if profile not in CHILD_PROFILES:
        raise KeyError(f"unknown child profile: {profile}")
    child_id = (
        f"{parent_task_id or 'child'}-sub{index}-{uuid.uuid4().hex[:6]}"
    )
    directory = (
        Path(ledger_dir)
        if ledger_dir is not None
        else Path.home() / ".xenon" / "ledger" / "children"
    )
    started = time.time()
    strategy = build_llm_loop_strategy(
        model, CHILD_PROFILES[profile], client=client
    )

    def _run() -> Any:
        return run_task(
            objective,
            user_objective=objective,
            strategy=strategy,
            workspace=workspace,
            ledger_dir=directory,
            task_id=child_id,
            permission=permission,
            # The parent (or the user) owns delivery; a child must not commit.
            finalize=False,
            max_steps=max_steps,
            gateway_context=gateway_context,
        )

    error: str | None = None
    run = None
    if timeout is None:
        try:
            run = _run()
        except Exception as exc:  # noqa: BLE001 — a child failure is data
            error = f"{type(exc).__name__}: {exc}"
    else:
        # A thread cannot be cancelled safely; the child is marked timed out and
        # its ledger remains the record of whatever it managed to do.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(_run)
        try:
            run = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            error = f"timeout after {timeout:.0f}s"
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        finally:
            pool.shutdown(wait=False)

    elapsed = round(time.time() - started, 3)
    ledger_path = directory / f"{child_id}.jsonl"
    if run is None:
        return ChildOutcome(
            child_id=child_id,
            objective=objective,
            profile=profile,
            accepted=False,
            phase="failed",
            answer="",
            ledger_path=str(ledger_path),
            debt=(),
            tool_calls=0,
            elapsed_seconds=elapsed,
            error=error or "child did not run",
        )
    result = run.result
    return ChildOutcome(
        child_id=child_id,
        objective=objective,
        profile=profile,
        accepted=result.accepted,
        phase=result.phase.value,
        answer=result.answer,
        ledger_path=str(run.ledger_path),
        debt=tuple(item.reason for item in result.debt),
        tool_calls=int(result.metrics.get("tool_calls", 0.0)),
        elapsed_seconds=elapsed,
        error=error,
    )


def run_child_tasks(
    *,
    objectives: Sequence[str],
    workspace: str | Path,
    model: ModelConfig,
    profile: str = "kernel",
    parent_task_id: str = "",
    ledger_dir: str | Path | None = None,
    permission: PermissionMode = PermissionMode.AUTO_EDIT,
    max_steps: int = 20,
    timeout: float | None = None,
    client: Any = None,
    max_workers: int = 4,
) -> tuple[ChildOutcome, ...]:
    """Run delegated tasks in parallel, preserving input order."""

    objectives = tuple(objective for objective in objectives if objective.strip())
    if not objectives:
        return ()
    workers = max(1, min(max_workers, len(objectives)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                run_child_task,
                objective=objective,
                workspace=workspace,
                model=model,
                profile=profile,
                parent_task_id=parent_task_id,
                ledger_dir=ledger_dir,
                permission=permission,
                max_steps=max_steps,
                timeout=timeout,
                client=client,
                index=index,
            )
            for index, objective in enumerate(objectives, 1)
        ]
        return tuple(future.result() for future in futures)


def child_evidence(outcome: ChildOutcome) -> dict[str, Any]:
    """Session-event payload for one child run (pointer, never a copy)."""

    return {
        "child_id": outcome.child_id,
        "objective": outcome.objective[:200],
        "profile": outcome.profile,
        "accepted": outcome.accepted,
        "phase": outcome.phase,
        "delivered": outcome.delivered,
        "ledger": outcome.ledger_path,
        "debt": list(outcome.debt),
        "tool_calls": outcome.tool_calls,
        "elapsed_seconds": outcome.elapsed_seconds,
        "error": outcome.error,
    }


__all__ = [
    "CHILD_PROFILES",
    "MAX_CHILD_DEPTH",
    "ChildOutcome",
    "child_evidence",
    "run_child_task",
    "run_child_tasks",
]
