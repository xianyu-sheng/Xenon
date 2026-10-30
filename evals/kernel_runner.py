"""Kernel-backed evaluation runner (#202).

The SWE-bench runner used to construct legacy engines and call ``engine.run``.
This module runs the same task through the single kernel loop instead, so the
published numbers are produced by the runtime that ships.

Nothing here re-implements the harness: ``run_task`` owns the ledger, policies,
verification and finalization.  The eval only supplies the workspace, the
sandbox runtime, the loop profile and the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from xenon.harness.llm import ModelConfig, build_llm_loop_strategy
from xenon.harness.runner import HarnessRun, run_task
from xenon.kernel.policies import PermissionMode
from xenon.kernel.strategies import LoopProfile

# Legacy eval names stay valid so archived result files remain comparable; each
# name now maps to a loop profile instead of an engine class.
EVAL_PROFILES: dict[str, LoopProfile] = {
    "react": LoopProfile(step_calls=1),
    "plan-execute": LoopProfile(plan=True, step_scope=True, follow_plan=True),
    "reflection": LoopProfile(reflect_rounds=1, reflect_calls=1),
    "plan-react": LoopProfile(plan=True, step_scope=True, step_calls=6),
    "plan-reflection": LoopProfile(
        plan=True,
        plan_review=True,
        step_scope=True,
        step_calls=6,
        step_reflect=True,
        reflect_rounds=1,
        reflect_calls=1,
    ),
    "react-reflection": LoopProfile(reflect_rounds=3, reflect_calls=2),
    "kernel": LoopProfile(plan=True, step_scope=True, step_calls=6),
}

CODE_EDITING_EVAL_NAMES: tuple[str, ...] = tuple(EVAL_PROFILES)


@dataclass(frozen=True, slots=True)
class KernelRunResult:
    """What the eval needs from one kernel turn."""

    answer: str
    accepted: bool
    phase: str
    tool_trace: tuple[dict[str, Any], ...]
    ledger_path: Path
    finalize_sha: str | None
    debt: tuple[str, ...]


def run_eval_task(
    *,
    objective: str,
    worktree: Path,
    model: ModelConfig,
    name: str = "kernel",
    max_steps: int = 40,
    permission: PermissionMode = PermissionMode.AUTO_EDIT,
    tool_runtime: Any = None,
    gateway_context: Any = None,
    constraints: Sequence[str] = (),
    task_id: str | None = None,
    ledger_dir: str | Path | None = None,
    client: Any = None,
) -> KernelRunResult:
    """Run one eval instance on the kernel loop."""

    profile = EVAL_PROFILES.get(name)
    if profile is None:
        raise KeyError(f"unknown eval profile: {name}")
    strategy = build_llm_loop_strategy(model, profile, client=client)
    run: HarnessRun = run_task(
        objective,
        user_objective=objective,
        strategy=strategy,
        workspace=worktree,
        task_id=task_id,
        ledger_dir=ledger_dir,
        permission=permission,
        # Evals grade the working tree; the finalizer must not create a commit
        # that would change what `git diff` reports.
        finalize=False,
        max_steps=max_steps,
        constraints=tuple(constraints),
        gateway_context=gateway_context,
        tool_runtime=tool_runtime,
    )
    return KernelRunResult(
        answer=run.result.answer,
        accepted=run.result.accepted,
        phase=run.result.phase.value,
        tool_trace=_tool_trace(run),
        ledger_path=run.ledger_path,
        finalize_sha=run.result.finalize_sha,
        debt=tuple(item.reason for item in run.result.debt),
    )


def _tool_trace(run: HarnessRun) -> tuple[dict[str, Any], ...]:
    """Receipt-level trace, in ledger order."""

    from xenon.ledger.events import EventKind
    from xenon.ledger.store import Ledger

    ledger = Ledger(run.ledger_path, run.task_id)
    trace: list[dict[str, Any]] = []
    for event in ledger.read(EventKind.TOOL_RECEIPT):
        receipt = event.payload.get("receipt") or {}
        if not isinstance(receipt, dict):
            continue
        trace.append(
            {
                "tool": receipt.get("tool") or "unknown",
                "success": str(receipt.get("status") or "") == "ok",
                "state": receipt.get("status"),
                "attempts": None,
                "elapsed_seconds": None,
                "error": receipt.get("error"),
                "result": str(receipt.get("summary") or "")[:500],
            }
        )
    return tuple(trace)


__all__ = [
    "CODE_EDITING_EVAL_NAMES",
    "EVAL_PROFILES",
    "KernelRunResult",
    "run_eval_task",
]
