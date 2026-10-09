"""Turn-flow pure helpers (R1: extracted from repl.py).

Deterministic budget/continuation helpers with no REPL state.
"""

from __future__ import annotations

import os
from typing import Any

_RETRY_BUDGET_MIN = 10
_RETRY_BUDGET_MAX = 40


def verify_retries_limit() -> int:
    """发布门重试上限：默认 2，XENON_VERIFY_RETRIES 可调（无效值回退默认，下限 0）。"""

    raw = os.environ.get("XENON_VERIFY_RETRIES", "").strip()
    if not raw:
        return 2
    try:
        return max(0, int(raw))
    except ValueError:
        return 2


def retry_budget_for(first_round_steps: int) -> int:
    """重试轮步数预算：首轮实际步数的一半，clamp 可配置（决策 2）。

    XENON_RETRY_BUDGET_MIN / XENON_RETRY_BUDGET_MAX（默认 10/40，无效值回退）。"""

    try:
        lo = int(os.environ.get("XENON_RETRY_BUDGET_MIN", "10"))
    except ValueError:
        lo = _RETRY_BUDGET_MIN
    try:
        hi = int(os.environ.get("XENON_RETRY_BUDGET_MAX", "40"))
    except ValueError:
        hi = _RETRY_BUDGET_MAX
    if hi < lo:
        hi = lo
    return max(lo, min(hi, max(1, first_round_steps) // 2))


def resume_prompt(node: Any) -> str:
    """续接提示：只陈述结构化事实（原因+指令），不猜测未记录内容。"""

    reason = node.verdict_reasons[0][:80] if node.verdict_reasons else node.status
    return (
        f"（续接上一轮任务：上一轮因「{reason}」未完成。"
        f"请先复核已完成的进度，再从中断处继续，最后给出结论。）\n"
    )
