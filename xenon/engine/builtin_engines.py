"""七种内置推理范式的注册声明。

This file only owns legacy engine-specific metadata (factory, mode line,
result title).  User-facing names/descriptions and kernel routing capabilities
live in :mod:`xenon.modes`; the descriptions are reused below so ``/mode``,
the setup wizard and the engine registry cannot drift.

引擎类采用函数内 import，保持惰性加载（首次真正运行该范式时才导入模块）。
"""

from __future__ import annotations

from typing import Any

from xenon.engine.registry import register_engine
from xenon.modes import MODE_CATALOG

# ── 标准步数预算 ────────────────────────────────────────
# 所有 step-based 引擎默认不按步数终止。`XENON_MAX_ITERATIONS` 可显式
# 设置安全上限；正常运行由 LoopDetector、无进展检查点、用户中断和墙钟
# deadline 控制。组合引擎内外层共享同一策略。
STANDARD_STEP_BUDGET = 0  # 0 = unlimited; LoopDetector/no-progress/deadline are the gates


def _description(name: str) -> str:
    """Read the canonical user-facing description from the mode catalog."""

    return MODE_CATALOG[name].description



def _step_budget(default: int) -> int:
    """单个循环的步数上限：`XENON_MAX_ITERATIONS` 可调，否则用传入的标准预算。"""

    import os

    raw = os.environ.get("XENON_MAX_ITERATIONS", "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


# ── 引擎工厂 ──────────────────────────────────────────────
# 每个 factory 只负责「这个范式独有的调参」；公共 kwargs（model_priority /
# model_pool / auto_router / callback / model_configs / permission_gate）由
# REPL 统一组装后透传。


def _make_react(**kwargs: Any) -> Any:
    from xenon.engine.react_engine import ReActEngine

    # 标准步数预算（120）：覆盖「读 → 改 → 验证 → 失败反馈 → 再改」的长循环；
    # 协议重试与压缩的额外轮次仍受 BudgetManager 的 3× 封顶约束，触顶由
    # `_mark_budget_exhausted` 诚实标注。
    return ReActEngine(max_iterations=_step_budget(STANDARD_STEP_BUDGET), **kwargs)


def _make_plan_execute(**kwargs: Any) -> Any:
    from xenon.engine.plan_execute_engine import PlanExecuteEngine

    return PlanExecuteEngine(
        max_steps=_step_budget(STANDARD_STEP_BUDGET),
        native_planning=True,
        **kwargs,
    )


def _make_reflection(**kwargs: Any) -> Any:
    from xenon.engine.reflection_engine import ReflectionEngine

    return ReflectionEngine(max_rounds=8, **kwargs)


def _make_plan_react(**kwargs: Any) -> Any:
    from xenon.engine.combined_engines import PlanReactEngine

    return PlanReactEngine(
        max_steps=_step_budget(STANDARD_STEP_BUDGET),
        react_iterations=_step_budget(STANDARD_STEP_BUDGET),
        native_planning=True,
        **kwargs,
    )


def _make_plan_reflection(**kwargs: Any) -> Any:
    from xenon.engine.combined_engines import PlanReflectionEngine

    return PlanReflectionEngine(
        max_steps=_step_budget(STANDARD_STEP_BUDGET),
        review_rounds=2,
        native_planning=True,
        **kwargs,
    )


def _make_react_reflection(**kwargs: Any) -> Any:
    from xenon.engine.combined_engines import ReactReflectionEngine

    return ReactReflectionEngine(
        react_iterations=_step_budget(STANDARD_STEP_BUDGET), review_rounds=2, **kwargs
    )


# ── 注册 ──────────────────────────────────────────────────

register_engine(
    "direct",
    factory=None,  # direct 不走引擎循环，REPL 单独分支处理
    description=_description("direct"),
)

register_engine(
    "react",
    factory=_make_react,
    description=_description("react"),
    mode_line="· ReAct 思考 → 行动 → 观察",
    result_title="ReAct 结果",
    # 重试/LLM 报错后若不保留面板，Ctrl+O 只剩原始日志，工具时间线看起来丢了。
    preserve_thinking_panel=True,
    # 用于排查空白面板根因。
    log_result_diagnostics=True,
)

register_engine(
    "plan-execute",
    factory=_make_plan_execute,
    description=_description("plan-execute"),
    mode_line="· Plan-Execute 规划 → 逐步执行",
    result_title="Plan-Execute 结果",
)

register_engine(
    "reflection",
    factory=_make_reflection,
    description=_description("reflection"),
    mode_line="· Reflection 执行 → 审查 → 修正",
    result_title="Reflection 结果",
)

register_engine(
    "plan-react",
    factory=_make_plan_react,
    description=_description("plan-react"),
    mode_line="· Plan+React 全局规划 → 每步 ReAct 执行",
    result_title="Plan+React 结果",
)

register_engine(
    "plan-reflection",
    factory=_make_plan_reflection,
    description=_description("plan-reflection"),
    mode_line="· Plan+Reflection 规划执行 → 反思修正",
    result_title="Plan+Reflection 结果",
)

register_engine(
    "react-reflection",
    factory=_make_react_reflection,
    description=_description("react-reflection"),
    mode_line="· React+Reflection 探索 → 反思审查",
    result_title="React+Reflection 结果",
)
