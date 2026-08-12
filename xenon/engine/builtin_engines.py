"""七种内置推理范式的注册声明 —— 引擎元数据的唯一来源。

这些 spec 此前散在四处：``repl.py`` 的 if/elif dispatch + 六个近乎一致的
``_run_*_engine`` 方法、``model_registry.BUILTIN_MODES`` 的描述文案、
``setup_wizard`` 的说明字典、``evals/runner.py`` 的引擎白名单。现在收敛到本文件。

引擎类采用函数内 import，保持惰性加载（首次真正运行该范式时才导入模块）。
"""

from __future__ import annotations

from typing import Any

from xenon.engine.registry import register_engine


# ── 引擎工厂 ──────────────────────────────────────────────
# 每个 factory 只负责「这个范式独有的调参」；公共 kwargs（model_priority /
# model_pool / auto_router / callback / model_configs / permission_gate）由
# REPL 统一组装后透传。


def _make_react(**kwargs: Any) -> Any:
    from xenon.engine.react_engine import ReActEngine

    # 普通对话任务可能涉及若干次「读 / 改 / 验证」循环。保留引擎自身的单次运行
    # 上限，同时给交互路径留出中等长度任务的空间；协议重试与压缩仍受
    # BudgetManager 的 2× 上限约束。
    return ReActEngine(max_iterations=40, **kwargs)


def _make_plan_execute(**kwargs: Any) -> Any:
    from xenon.engine.plan_execute_engine import PlanExecuteEngine

    return PlanExecuteEngine(max_steps=40, **kwargs)


def _make_reflection(**kwargs: Any) -> Any:
    from xenon.engine.reflection_engine import ReflectionEngine

    return ReflectionEngine(max_rounds=8, **kwargs)


def _make_plan_react(**kwargs: Any) -> Any:
    from xenon.engine.combined_engines import PlanReactEngine

    return PlanReactEngine(max_steps=24, react_iterations=24, **kwargs)


def _make_plan_reflection(**kwargs: Any) -> Any:
    from xenon.engine.combined_engines import PlanReflectionEngine

    return PlanReflectionEngine(max_steps=24, review_rounds=2, **kwargs)


def _make_react_reflection(**kwargs: Any) -> Any:
    from xenon.engine.combined_engines import ReactReflectionEngine

    return ReactReflectionEngine(react_iterations=24, review_rounds=2, **kwargs)


# ── 注册 ──────────────────────────────────────────────────

register_engine(
    "direct",
    factory=None,  # direct 不走引擎循环，REPL 单独分支处理
    description="直接对话，不使用特殊引擎（默认模式）",
)

register_engine(
    "react",
    factory=_make_react,
    description="思考-行动-观察循环，适合需要工具的探索性任务",
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
    description="先用强模型规划，再逐步执行，适合复杂任务",
    mode_line="· Plan-Execute 规划 → 逐步执行",
    result_title="Plan-Execute 结果",
)

register_engine(
    "reflection",
    factory=_make_reflection,
    description="执行后自我审查并修正，适合高质量代码生成",
    mode_line="· Reflection 执行 → 审查 → 修正",
    result_title="Reflection 结果",
)

register_engine(
    "plan-react",
    factory=_make_plan_react,
    description="全局规划 + 每步 ReAct 执行，适合复杂多步骤任务",
    mode_line="· Plan+React 全局规划 → 每步 ReAct 执行",
    result_title="Plan+React 结果",
)

register_engine(
    "plan-reflection",
    factory=_make_plan_reflection,
    description="规划执行 + 反思修正，适合需要高质量输出的任务",
    mode_line="· Plan+Reflection 规划执行 → 反思修正",
    result_title="Plan+Reflection 结果",
)

register_engine(
    "react-reflection",
    factory=_make_react_reflection,
    description="ReAct 探索 + 反思审查，适合需要工具且要求高质量的任务",
    mode_line="· React+Reflection 探索 → 反思审查",
    result_title="React+Reflection 结果",
)
