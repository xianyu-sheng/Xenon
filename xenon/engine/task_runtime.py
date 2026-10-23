"""One engine-layer delivery exit shared by all engine families.

BaseEngine subclasses and the combined engines have different internals but
the same external contract: ``run()`` returns a final answer.  This runtime
wraps that black box, folds verified calls into TaskState, and re-enters the
same engine with a continuation prompt when required delivery work is missing.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_IGNORED_DIRTY_RE = re.compile(
    r"^\?\? (?:.*/)?(?:__pycache__/|\.pytest_cache/)|^\?\? .*\.pyc$"
)


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
        if line.strip() and not _IGNORED_DIRTY_RE.search(line)
    ]
    from xenon.engine.task_state import _status_breakdown

    all_paths, staged, unstaged = _status_breakdown(chr(10).join(lines))
    return bool(lines), all_paths, staged, unstaged


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
        if state.commit:
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
