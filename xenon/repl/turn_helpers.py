"""Turn-flow pure helpers (R1: extracted from repl.py).

Deterministic budget/continuation helpers with no REPL state.
"""

from __future__ import annotations

import os
import re
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


def panel_step_paths(step: Any) -> list[str]:
    """从单个面板步骤提取文件路径（写/改/批量），供产物追踪复用。"""

    ai = getattr(step, "action_input", {}) or {}
    if not isinstance(ai, dict):
        return []
    paths: list[str] = []
    for key in ("file_path", "file_paths", "path", "target_directory"):
        val = ai.get(key)
        if isinstance(val, str):
            paths.append(val)
        elif isinstance(val, list):
            paths.extend([str(v) for v in val if isinstance(v, str)])
    if getattr(step, "action", "") == "batch_write" and "files" in ai:
        files = ai["files"]
        if isinstance(files, list):
            for f in files:
                if isinstance(f, dict) and "path" in f:
                    paths.append(str(f["path"]))
    return paths


def paths_from_panel(panel: Any) -> list[str]:
    """回合内成功写入/修改的文件路径（去重保序，最新在前）。"""

    found: list[str] = []
    for step in getattr(panel, "steps", []) or []:
        if getattr(step, "is_error", False) or not getattr(step, "action", None):
            continue
        for p in panel_step_paths(step):
            if p not in found:
                found.append(p)
    return found


# ── 外部信息查询特征（通用语言结构，R1-2 从 repl.py 迁移）──

_RE_QUESTION_STRUCTURE = re.compile(
    r"[吗呢吧啊][？?]?$"  # 句末疑问语气词
    r"|[？?]$"  # 问号结尾
    r"|有没有|会不会|能不能|可不可以"  # 正反问结构
    r"|怎么(?:走|去|办|样|回事)"  # 疑问代词 + 动作
    r"|在哪里|在哪|什么时候|几点|多少"  # 疑问短语
    r"|what|when|where|how|which|who",  # 英文疑问词
    re.IGNORECASE,
)
_RE_QUERY_VERB = re.compile(
    r"(?:帮|请|给).{0,3}(?:我)?(?:查|搜|找|查询|搜索|查找|看看|了解)"  # 委托查询
    r"|^(?:查|搜|找|查询|搜索|查找|看看)"  # 句首查询动词
    r"|(?:search|find|look\s*up|check|query)\s",  # 英文查询
    re.IGNORECASE,
)
_RE_TIME_SENSITIVE = re.compile(
    r"(?:今天|今日|现在|目前|最近|这周末|本周|下周|本月|这个月"
    r"|明天|后天|昨天|周日|周一|周二|周三|周四|周五|周六"
    r"|today|now|recently|this\s+week|next\s+week|tomorrow)",
    re.IGNORECASE,
)
# 排除：明确是关于代码/文件的查询（由 正则信号层 处理）
_RE_CODE_CONTEXT = re.compile(
    r"(?:文件|代码|项目|脚本|程序|函数|类|目录|文件夹|bug|错误|报错"
    r"|测试|配置|日志|commit|分支|仓库|git\b"
    r"|\.(?:py|js|ts|java|go|rs|cpp|c|h|html|css|json|yaml|yml|toml|md|txt|sh)\b)",
    re.IGNORECASE,
)



def _looks_like_external_query(text: str) -> bool:
    """通用判断：输入是否具有"外部信息查询"特征。

    基于语言结构而非领域关键词：
    - 疑问结构（吗/呢/？/有没有/怎么走/在哪里/什么时候/几点/多少）
    - 查询动词（查/搜/找/search/find）
    - 时间敏感框架（今天/明天/最近...）

    排除：明确关于代码/文件的查询（由 正则信号层 处理）。
    """
    if not text or len(text) < 3:
        return False
    # 代码相关 → 不归这里管
    if _RE_CODE_CONTEXT.search(text):
        return False
    # 疑问结构 → 需要外部信息
    if _RE_QUESTION_STRUCTURE.search(text):
        return True
    # 查询动词 → 在搜索/查找信息
    if _RE_QUERY_VERB.search(text):
        return True
    # 时间敏感短语 → 大概率需要实时/外部数据
    if _RE_TIME_SENSITIVE.search(text):
        return True
    return False


