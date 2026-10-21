"""One engine-layer delivery exit shared by all engine families.

BaseEngine subclasses and the combined engines have different internals but
the same external contract: ``run()`` returns a final answer.  This runtime
wraps that black box, folds verified calls into TaskState, and re-enters the
same engine with a continuation prompt when required delivery work is missing.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def run_with_delivery(
    engine: Any,
    user_input: str,
    context: Any | None = None,
    ctx_mgr: Any | None = None,
    *,
    max_delivery_retries: int = 3,
) -> str:
    """Run an engine once, then enforce the durable delivery contract."""
    from xenon.engine.result import TerminationReason, build_agent_result
    from xenon.engine.task_state import TaskState
    from xenon.engine.tool_tracker import ToolExecutionTracker

    try:
        result = engine.run(user_input, context=context, ctx_mgr=ctx_mgr)
    except Exception:
        # Preserve the stack for long-running tasks; callers otherwise only
        # see a bare message after hundreds of tool calls.
        logger.exception("engine.run failed before delivery finalization")
        raise
    state = TaskState.from_context(context)
    if state is None or not state.active:
        final_result = build_agent_result(engine, result, context)
        engine.last_result = final_result
        return final_result

    aggregate = ToolExecutionTracker()
    tracker = getattr(engine, "_last_tracker", None)
    if tracker is not None:
        aggregate.calls.extend(list(getattr(tracker, "calls", []) or []))

    def _refresh() -> list[str]:
        state.update_from_calls(aggregate.calls)
        missing = state.missing_delivery()
        if not missing:
            state.maybe_complete()
        context.set("_task_state", state.to_dict())
        return missing

    attempts = 0
    while True:
        missing = _refresh()
        if not missing:
            break
        if attempts >= max_delivery_retries:
            labels = {
                "verify": "测试",
                "changes": "实际源码修改",
                "commit": "commit",
                "pr": "PR",
            }
            result += (
                "\n\n⚠️ **任务交付未完成**：缺少 "
                + "、".join(labels[m] for m in missing)
                + "。以上工作不应视为已完成。"
            )
            engine._last_termination_reason = TerminationReason.DELIVERY_INCOMPLETE
            engine.callback.on_warning(
                "引擎交付重试上限已到，最终回答附未完成交付警告"
            )
            break
        attempts += 1
        labels = {
            "verify": "运行相关测试并记录真实结果",
            "changes": "实际修改非测试源码文件，不能只提交空改动或测试",
            "commit": "创建 git commit",
            "pr": "push 并创建 PR",
        }
        continuation = (
            "继续完成当前任务的交付，不要重新做一遍已经完成的工作。\n"
            "尚未完成："
            + "；".join(labels[m] for m in missing)
            + "。请实际执行这些动作，完成后给出最终回答。\n\n"
            + state.delivery_prompt()
        )
        engine.callback.on_warning(
            "引擎交付未完成，重新进入同一执行器补齐 "
            + "、".join(missing)
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

    engine._last_tracker = aggregate
    if getattr(engine, "_last_termination_reason", None) is None:
        engine._last_termination_reason = TerminationReason.FINAL
    final_result = build_agent_result(engine, result, context)
    engine.last_result = final_result
    return final_result
