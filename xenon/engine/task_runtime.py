"""One engine-layer delivery exit shared by all engine families.

BaseEngine subclasses and the combined engines have different internals but
the same external contract: ``run()`` returns a final answer.  This runtime
wraps that black box, folds verified calls into TaskState, and re-enters the
same engine with a continuation prompt when required delivery work is missing.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from xenon.engine.evidence_contracts import IGNORED_DIRTY_RE, paths_match

logger = logging.getLogger(__name__)


def _engine_workspace_root(
    engine: Any, context: Any | None = None
) -> "Path | None":
    """Best-effort trusted worktree root for a deterministic git probe."""
    if context is not None and hasattr(context, "get"):
        explicit = context.get("_workspace_root")
        if explicit:
            return Path(str(explicit))
    try:
        from xenon.engine.execution_policy import walk_engine_graph

        for node in walk_engine_graph(engine):
            runtime = getattr(node, "tool_runtime", None)
            root = getattr(runtime, "workspace_root", None)
            if root:
                return Path(root)
    except Exception:  # noqa: BLE001 — probe is optional
        logger.debug("workspace root probe failed", exc_info=True)
    return None


def _probe_git_dirty(
    engine: Any, context: Any | None = None
) -> "tuple[bool | None, list[str], list[str], list[str]]":
    """Return the real worktree status, ignoring generated caches.

    The model can claim ``git status`` is clean without running it after its
    last write.  Delivery must check the worktree itself.  Unrelated caches
    are ignored so the gate does not fail on pytest/bytecode noise; source or
    backup files are not ignored.
    """
    import subprocess

    root = _engine_workspace_root(engine, context)
    if root is None:
        return None, [], [], []
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "-uall"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None, [], [], []
    if proc.returncode != 0:
        return None, [], [], []
    lines = [
        line
        for line in proc.stdout.splitlines()
        if line.strip() and not IGNORED_DIRTY_RE.search(line)
    ]
    from xenon.engine.task_state import _status_breakdown

    all_paths, staged, unstaged = _status_breakdown(chr(10).join(lines))
    return bool(lines), all_paths, staged, unstaged


def record_delivery_fact(
    ledger: Any,
    state: Any,
    *,
    missing: list[str],
    attempt: int = 0,
) -> Any:
    """Append the durable delivery snapshot used by replay/projection.

    This is a fact record, not a second state store: the payload mirrors the
    fields TaskState derives from tool events and the real worktree probe.
    Secrets, prompts and file contents are deliberately excluded.
    """
    if ledger is None or not hasattr(ledger, "append"):
        return None
    from xenon.engine.evidence_runtime import (
        EventKind,
        EvidenceSource,
        LifecyclePhase,
    )

    payload = {
        "task_id": getattr(state, "task_id", ""),
        "attempt": int(attempt or 0),
        "missing": list(missing),
        "dirty": getattr(state, "dirty_after_commit", None),
        "dirty_paths": list(getattr(state, "dirty_paths", []) or []),
        "dirty_staged_paths": list(
            getattr(state, "dirty_staged_paths", []) or []
        ),
        "dirty_unstaged_paths": list(
            getattr(state, "dirty_unstaged_paths", []) or []
        ),
        "commit": str(getattr(state, "commit", "") or ""),
        "committed_paths": list(getattr(state, "committed_artifacts", []) or []),
        "committed_source_paths": list(
            getattr(state, "committed_source_paths", []) or []
        ),
        "committed_revision": int(getattr(state, "committed_revision", 0) or 0),
        "tests": list(getattr(state, "tests", []) or []),
        "failed_tests": list(getattr(state, "failed_tests", []) or []),
        "verification_failures": [
            {
                "command": str(item.get("command") or "")[:240],
                "exit_code": item.get("exit_code"),
            }
            for item in (getattr(state, "verification_failures", []) or [])
            if isinstance(item, dict)
        ],
        "artifacts_revision": int(getattr(state, "artifacts_revision", 0) or 0),
        "verified_revision": int(getattr(state, "verified_revision", 0) or 0),
        "status": str(getattr(state, "status", "active") or "active"),
    }
    return ledger.append(
        LifecyclePhase.DELIVERY,
        EventKind.DELIVERY,
        EvidenceSource.GATE,
        payload,
    )


def _finalize_enabled() -> bool:
    """Auto-finalize is on by default; it can be disabled explicitly."""
    value = os.environ.get("XENON_AUTO_FINALIZE", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def finalize_verified_work(
    state: Any,
    workspace_root: Path | str | None,
) -> dict[str, Any]:
    """Commit verified, task-owned staged paths without touching anything else.

    This is intentionally narrower than a general ``git add -A && commit``:
    every dirty path must already be staged and must match an artifact written
    by this task.  Any unrelated or unstaged path makes the result ``blocked``
    so the normal delivery continuation remains responsible for it.
    """
    if not _finalize_enabled():
        return {"status": "disabled", "reason": "XENON_AUTO_FINALIZE=0"}
    if state is None or not getattr(state, "active", False):
        return {"status": "skipped", "reason": "inactive task"}
    if "commit" not in (getattr(state, "required", []) or []):
        return {"status": "skipped", "reason": "commit not required"}
    if not (getattr(state, "tests", []) or []):
        return {"status": "blocked", "reason": "no successful verification"}
    if int(getattr(state, "verified_revision", 0) or 0) < int(
        getattr(state, "artifacts_revision", 0) or 0
    ):
        return {"status": "blocked", "reason": "verification is stale"}
    if getattr(state, "verification_failures", []):
        return {"status": "blocked", "reason": "latest verification failed"}
    staged = list(getattr(state, "dirty_staged_paths", []) or [])
    unstaged = list(getattr(state, "dirty_unstaged_paths", []) or [])
    if not staged:
        return {"status": "skipped", "reason": "no staged paths"}
    if unstaged:
        return {
            "status": "blocked",
            "reason": "unstaged paths remain: " + ", ".join(unstaged[:8]),
        }
    artifacts = list(getattr(state, "artifacts", []) or [])
    allowed = list(getattr(state, "test_artifacts", []) or []) + artifacts
    unrelated = [
        path
        for path in staged
        if not any(paths_match(path, candidate) for candidate in allowed)
    ]
    if unrelated:
        return {
            "status": "blocked",
            "reason": "staged unrelated paths: " + ", ".join(unrelated[:8]),
        }
    if not getattr(state, "has_source_artifact", lambda: False)():
        return {"status": "blocked", "reason": "no non-test source artifact"}
    if workspace_root is None:
        return {"status": "blocked", "reason": "workspace root unavailable"}
    root = Path(workspace_root)
    if not root.is_dir():
        return {"status": "blocked", "reason": "workspace root is not a directory"}

    from xenon.utils.attribution import TRAILER, attribution_enabled

    argv = ["git", "-C", str(root), "commit", "-m", "chore: finalize verified changes"]
    if attribution_enabled():
        argv[4:4] = ["--trailer", TRAILER]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            cwd=str(root),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "reason": str(exc)}
    if proc.returncode != 0:
        return {
            "status": "error",
            "reason": (proc.stdout + proc.stderr).strip()[-800:],
        }

    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            cwd=str(root),
        )
        commit_hash = (
            head.stdout.strip().splitlines()[0] if head.returncode == 0 else ""
        )
    except (OSError, subprocess.SubprocessError):
        commit_hash = ""
    if not commit_hash:
        return {
            "status": "error",
            "reason": "git commit succeeded but HEAD hash could not be read",
        }
    committed_paths = staged
    try:
        shown = subprocess.run(
            ["git", "-C", str(root), "show", "--pretty=format:", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            cwd=str(root),
        )
        if shown.returncode == 0:
            committed_paths = [
                line.strip() for line in shown.stdout.splitlines() if line.strip()
            ]
    except (OSError, subprocess.SubprocessError):
        pass

    # Use the same durable ordering as a real git tool call: commit hash first,
    # then the real path set, then clear the worktree facts.  A direct private
    # helper is deliberate here: this is finalization of already-verified work,
    # not a new model tool invocation.
    state._record_commit(  # noqa: SLF001 — same package transaction
        commit_hash, {"committed_paths": committed_paths}
    )
    state.dirty_after_commit = False
    state.dirty_paths = []
    state.dirty_staged_paths = []
    state.dirty_unstaged_paths = []
    state.updated_at = __import__("time").time()
    return {
        "status": "committed",
        "commit": commit_hash,
        "paths": committed_paths,
        "message": (proc.stdout + proc.stderr).strip()[-800:],
    }


def run_with_delivery(
    engine: Any,
    user_input: str,
    context: Any | None = None,
    ctx_mgr: Any | None = None,
    *,
    max_delivery_retries: int = 3,
) -> str:
    """Run an engine once, then enforce the durable delivery contract."""
    from xenon.engine.evidence_runtime import EventKind
    from xenon.engine.result import TerminationReason, build_agent_result
    from xenon.engine.task_state import TaskState
    from xenon.engine.task_state_projection import TaskStateProjection
    from xenon.engine.tool_tracker import ToolExecutionTracker

    initial_state = TaskState.from_context(context)
    projection: TaskStateProjection | None = None
    live_projection = False
    ledger = context.get("_evidence_ledger") if context is not None else None
    if (
        initial_state is not None
        and initial_state.active
        and ledger is not None
        and hasattr(ledger, "events")
    ):
        # Subscribe before the engine emits this run's plan/step events, so a
        # UI subscriber can render the plan while work is happening instead of
        # only after the final answer.
        def _notify_plan_state(state: TaskState) -> None:
            bus = getattr(engine, "event_bus", None)
            if bus is None:
                return
            try:
                bus.publish(
                    "plan_state",
                    state.plan_summary(),
                    state.to_dict(),
                )
            except Exception:  # noqa: BLE001 — UI channel must not break work
                logger.debug("plan_state publish failed", exc_info=True)

        projection = TaskStateProjection(
            initial_state, context, on_change=_notify_plan_state
        )
        # Replay any already-recorded events for this task (resume/retry),
        # then stay subscribed for the new events emitted by this run.
        projection.attach(ledger, replay=True)
        live_projection = True

    try:
        result = engine.run(user_input, context=context, ctx_mgr=ctx_mgr)
    except Exception:
        if projection is not None:
            projection.close()
        # Preserve the stack for long-running tasks; callers otherwise only
        # see a bare message after hundreds of tool calls.
        logger.exception("engine.run failed before delivery finalization")
        raise
    state = (
        initial_state
        if live_projection and initial_state is not None
        else (TaskState.from_context(context) or initial_state)
    )
    # The engine may have created/bound the ledger during run().  Refresh the
    # reference before deciding whether a post-run replay is needed.
    if context is not None and hasattr(context, "get"):
        ledger = context.get("_evidence_ledger")
    if state is None or not state.active:
        if live_projection and projection is not None:
            projection.close()
        final_result = build_agent_result(engine, result, context)
        engine.last_result = final_result
        return final_result

    aggregate = ToolExecutionTracker()
    tracker = getattr(engine, "_last_tracker", None)
    if tracker is not None:
        aggregate.calls.extend(list(getattr(tracker, "calls", []) or []))

    workspace_root = _engine_workspace_root(engine, context)

    def _audit(attempt: int = 0) -> list[Any]:
        """Run the criterion-level goal audit and persist its verdicts.

        Only ``rejected`` criteria are hard failures; ``unverifiable`` is
        recorded for honesty but does not force an endless repair loop when a
        workspace probe is unavailable.
        """
        from xenon.engine.claim_gate import audit_goal, record_claim_verdicts

        verdicts = audit_goal(state, workspace_root=workspace_root)
        if not verdicts:
            return []
        if ledger is not None and hasattr(ledger, "append"):
            record_claim_verdicts(
                ledger,
                verdicts,
                task_id=state.task_id,
                attempt=attempt,
            )
        else:
            for verdict in verdicts:
                state.record_criterion(verdict.to_dict())
            if context is not None and hasattr(context, "set"):
                context.set("_task_state", state.to_dict())
        return [v for v in verdicts if v.status == "rejected"]

    # The evidence ledger is the durable source.  If we could not subscribe
    # before the run, replay its tool observations into TaskState when we can
    # identify this task's boundary; keep the tracker fallback otherwise.
    use_projection = live_projection
    if not live_projection and ledger is not None and hasattr(ledger, "events"):
        start_sequence = 0
        for event in getattr(ledger, "events", []):
            if (
                event.kind == EventKind.TASK_FACT.value
                and event.payload.get("task_id") == state.task_id
            ):
                start_sequence = event.sequence
                break
        projection = TaskStateProjection(state, context)
        if start_sequence:
            projection.attach(
                ledger,
                start_sequence=start_sequence,
                replay=True,
            )
            use_projection = True
        else:
            # No boundary marker: let the tracker fill the current run and
            # project future events only (no historical cross-task replay).
            projection.attach(ledger, replay=False)

    def _refresh() -> tuple[list[str], list[Any]]:
        if not use_projection:
            state.update_from_calls(aggregate.calls)
        (
            actual_dirty,
            dirty_paths,
            staged_paths,
            unstaged_paths,
        ) = _probe_git_dirty(engine, context)
        if actual_dirty is not None and (
            state.commit or staged_paths or unstaged_paths
        ):
            state.dirty_after_commit = actual_dirty
            state.dirty_paths = dirty_paths
            state.dirty_staged_paths = staged_paths
            state.dirty_unstaged_paths = unstaged_paths
        finalized = finalize_verified_work(state, workspace_root)
        if finalized.get("status") == "committed":
            logger.info(
                "deterministic finalize committed %s (%s paths)",
                str(finalized.get("commit", ""))[:12],
                len(finalized.get("paths", [])),
            )
            # Final completion must be based on the real clean tree, not on
            # the fact that git accepted the commit.
            (
                actual_dirty,
                dirty_paths,
                staged_paths,
                unstaged_paths,
            ) = _probe_git_dirty(engine, context)
            if actual_dirty is not None:
                state.dirty_after_commit = actual_dirty
                state.dirty_paths = dirty_paths
                state.dirty_staged_paths = staged_paths
                state.dirty_unstaged_paths = unstaged_paths
        elif finalized.get("status") in {"blocked", "error"}:
            logger.debug(
                "deterministic finalize %s: %s",
                finalized.get("status"),
                finalized.get("reason"),
            )
        missing = state.missing_delivery()
        goal_failures: list[Any] = []
        if missing:
            state.reconcile_delivery_steps(missing)
        else:
            # Delivery facts are complete.  Audit the actual task goal at
            # criterion granularity instead of trusting the model's summary.
            goal_failures = _audit(attempts)
            if not goal_failures:
                state.maybe_complete()
        record_delivery_fact(
            ledger,
            state,
            missing=missing,
            attempt=attempts,
        )
        context.set("_task_state", state.to_dict())
        return missing, goal_failures

    attempts = 0
    while True:
        missing, goal_failures = _refresh()
        if not missing and not goal_failures:
            break
        if attempts >= max_delivery_retries:
            labels = {
                "test": "真实回归测试",
                "verify": "测试",
                "changes": "实际源码修改",
                "commit": "commit",
                "dirty": "工作区未提交改动",
                "pr": "PR",
            }
            warning_parts = []
            if missing:
                warning_parts.append(
                    "缺少 " + "、".join(labels[m] for m in missing)
                )
            if goal_failures:
                from xenon.engine.claim_gate import goal_failure_prompt

                warning_parts.append(
                    "目标条件未通过：\n"
                    + goal_failure_prompt(goal_failures)
                )
            result += (
                "\n\n⚠️ **任务交付未完成**："
                + "；".join(warning_parts)
                + "。以上工作不应视为已完成。"
            )
            engine._last_termination_reason = TerminationReason.DELIVERY_INCOMPLETE
            engine.callback.on_warning(
                "引擎交付重试上限已到，最终回答附未完成交付警告"
            )
            break
        attempts += 1
        labels = {
            "test": "新增非空回归测试文件并真实运行",
            "verify": "运行相关测试并记录真实结果",
            "changes": "实际修改非测试源码文件，不能只提交空改动或测试",
            "commit": "创建 git commit",
            "dirty": "确认 commit 后工作区干净，没有未提交改动",
            "pr": "push 并创建 PR",
        }
        if goal_failures:
            from xenon.engine.claim_gate import goal_failure_prompt

            continuation = (
                "最终目标校验未通过。只修复下面这些具体条件，不要重做已经完成"
                "的工作；修复后重新验证并完成交付。\n\n"
                + goal_failure_prompt(goal_failures)
                + "\n\n"
                + state.delivery_prompt()
            )
        elif "verify" in missing and state.verification_failures:
            continuation = (
                "验证失败。不要重新规划或重做已经成功的工作；先修复下面的真实"
                "错误输出，然后重跑同一验证命令，并完成 commit/工作区清理。\n\n"
                + state.delivery_prompt()
            )
        elif "dirty" in missing and state.dirty_paths:
            if state.dirty_staged_paths and not state.dirty_unstaged_paths:
                continuation = (
                    "交付仍未完成：以下修改已经暂存但还没有 commit：\n- "
                    + "\n- ".join(state.dirty_staged_paths[:20])
                    + "\n请直接创建 commit，然后确认工作区为空；不要再修改文件。\n\n"
                    + state.delivery_prompt()
                )
            else:
                continuation = (
                    "交付仍未完成：工作区还有未清理路径：\n- "
                    + "\n- ".join(state.dirty_paths[:20])
                    + "\n请先实际删除本轮产生的备份/临时文件，或把它们纳入提交；"
                    "不要只运行 git status 后声称干净。完成后再确认工作区为空。\n\n"
                    + state.delivery_prompt()
                )
        else:
            continuation = (
                "继续完成当前任务的交付，不要重新做一遍已经完成的工作。\n"
                "尚未完成："
                + "；".join(labels[m] for m in missing)
                + "。请实际执行这些动作，完成后给出最终回答。\n\n"
                + state.delivery_prompt()
            )
        pending_labels = list(missing)
        if goal_failures:
            pending_labels.append(
                "目标条件 "
                + ", ".join(v.claim.claim_id for v in goal_failures[:3])
            )
        engine.callback.on_warning(
            "引擎交付未完成，重新进入同一执行器补齐 "
            + "、".join(pending_labels)
            + f" ({attempts}/{max_delivery_retries})"
        )
        try:
            result = engine.run(continuation, context=context, ctx_mgr=ctx_mgr)
        except Exception as exc:  # noqa: BLE001 — keep the partial result
            logger.warning("交付续跑失败，保留上一轮结果: %s", exc)
            break
        tracker = getattr(engine, "_last_tracker", None)
        if tracker is not None:
            aggregate.calls.extend(list(getattr(tracker, "calls", []) or []))

    if projection is not None:
        projection.close()
    engine._last_tracker = aggregate
    if getattr(engine, "_last_termination_reason", None) is None:
        engine._last_termination_reason = TerminationReason.FINAL
    final_result = build_agent_result(engine, result, context)
    engine.last_result = final_result
    return final_result
