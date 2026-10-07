"""Deterministic cross-turn task-state block for the classifier.

One compact, budget-bounded text assembled from *structured* state (goal,
pending gate, recent artifacts, plan mode).  It replaces scattered regex
cross-turn detection: the LLM sees the same state a careful human would, and
the merge layer executes whatever it binds.  The block never carries
authority — it lists facts; permissions stay in the tool-boundary layer.
"""

from __future__ import annotations

from typing import Any


def build_task_state_block(
    *,
    active_goal: Any = None,
    pending: Any = None,
    artifacts: list[str] | tuple[str, ...] = (),
    plan_mode: bool = False,
    budget: int = 300,
) -> str:
    """Render the block; returns "" when there is nothing to say."""

    lines: list[str] = []
    if active_goal is not None and getattr(active_goal, "objective", ""):
        lines.append(f"当前目标: {str(active_goal.objective)[:100]}")
    if pending is not None:
        gate_id = getattr(pending, "gate_id", "") or ""
        lines.append(
            "打开的门: "
            f"id={gate_id} | "
            f"{getattr(pending, 'kind', 'approval')} | "
            f"{str(getattr(pending, 'reason', ''))[:60]} | "
            f"待授权操作: {sorted(getattr(pending, 'operations', ()))}"
        )
    if plan_mode:
        lines.append("计划模式: 开启（写/执行需用户确认）")
    artifacts = list(artifacts)[-3:]
    if artifacts:
        lines.append("最近产物: " + "；".join(str(a)[:80] for a in artifacts))
    if not lines:
        return ""

    block = "## 任务状态（跨轮次）\n" + "\n".join(f"- {line}" for line in lines)
    if len(block) > budget:
        keep: list[str] = []
        used = len("## 任务状态（跨轮次）")
        for line in lines:
            item = f"- {line}"
            if used + len(item) + 1 > budget:
                break
            keep.append(item)
            used += len(item) + 1
        block = "## 任务状态（跨轮次）\n" + "\n".join(keep)
    return block
