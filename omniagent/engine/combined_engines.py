"""
Combined Engines — 思考范式组合引擎。

将多种思考范式组合使用，发挥各自优势：
- PlanReactEngine: 全局规划 + 每步 ReAct 执行
- PlanReflectionEngine: 规划执行 + 反思修正
- ReactReflectionEngine: ReAct 探索 + 反思审查
"""

from __future__ import annotations

import logging
from typing import Any

from omniagent.engine.context import AgentContext
from omniagent.engine.plan_execute_engine import PlanExecuteEngine
from omniagent.engine.react_engine import ReActEngine, BUILTIN_TOOLS
from omniagent.engine.reflection_engine import ReflectionEngine
from omniagent.utils.llm_client import chat_completion

logger = logging.getLogger(__name__)


class PlanReactEngine:
    """
    Plan + React 组合引擎。

    策略：用 Plan-Execute 做全局规划，每个步骤用 ReAct 循环执行。
    适合需要既有宏观规划、又有灵活工具调用的复杂任务。
    """

    def __init__(
        self,
        model_priority: list[str],
        *,
        max_steps: int = 15,
        react_iterations: int = 5,
    ) -> None:
        self.model_priority = model_priority
        self.max_steps = max_steps
        self.react_iterations = react_iterations
        self.planner = PlanExecuteEngine(model_priority, max_steps=max_steps)
        self.reactor = ReActEngine(model_priority, max_iterations=react_iterations)

    def run(self, user_input: str, context: AgentContext | None = None) -> str:
        from rich.console import Console
        console = Console()

        ctx = context or AgentContext()

        # Phase 1: 全局规划
        console.print("[dim]📋 Phase 1: 生成执行计划...[/dim]")
        try:
            plan = self.planner._plan(user_input, ctx)
        except Exception as e:
            console.print(f"[red]  ✗ 规划阶段异常: {e}[/red]")
            return f"规划阶段失败: {e}"
        steps = plan.get("steps", [])
        analysis = plan.get("analysis", "")
        logger.info(f"Plan 结果: steps={len(steps)}, analysis={analysis[:200] if analysis else '(空)'}")

        if not steps:
            console.print(f"[yellow]  ⚠ 未生成步骤，analysis={analysis[:200] if analysis else '(空)'}[/yellow]")
            return analysis or "未能生成有效的执行计划。"

        console.print(f"[dim]📋 计划生成 {len(steps)} 个步骤[/dim]")

        # 显示计划
        for s in steps:
            console.print(f"  [dim]步骤 {s.get('id', '?')}: {s.get('task', '')}[/dim]")

        # Phase 2: 每步用 ReAct 执行
        console.print(f"\n[dim]🔄 Phase 2: ReAct 逐步执行[/dim]")
        results = []

        for i, step in enumerate(steps[:self.max_steps]):
            step_task = step.get("task", "")
            step_id = step.get("id", i + 1)

            console.print(f"\n[cyan]🔄 步骤 {step_id}/{len(steps)}: {step_task}[/cyan]")

            # 构建包含全局上下文的 ReAct 输入
            prev_context = ""
            if results:
                prev_context = "\n之前步骤的结果:\n" + "\n".join(
                    f"- 步骤 {r['step_id']}: {r['result'][:150]}"
                    for r in results[-3:]
                )

            react_input = (
                f"全局任务: {user_input}\n"
                f"当前步骤 ({step_id}/{len(steps)}): {step_task}"
                f"{prev_context}"
            )

            # 用 ReAct 执行当前步骤
            try:
                step_result = self.reactor.run(react_input, context=ctx)
                if not step_result or not step_result.strip():
                    step_result = f"(步骤 {step_id} 执行完成，无文本输出)"
                console.print(f"[green]  ✓ 步骤 {step_id} 完成 ({len(step_result)} 字符)[/green]")
            except Exception as e:
                step_result = f"步骤执行失败: {e}"
                console.print(f"[red]  ✗ 步骤 {step_id} 失败: {e}[/red]")

            results.append({
                "step_id": step_id,
                "task": step_task,
                "result": step_result,
            })

            # 存入上下文
            ctx.set(f"step_{step_id}_result", step_result)

        # Phase 3: 汇总
        console.print(f"\n[dim]📝 Phase 3: 汇总结果...[/dim]")
        summary = self._summarize(user_input, results, analysis)
        return summary

    def _summarize(self, user_input: str, results: list[dict], analysis: str = "") -> str:
        """汇总所有步骤的结果。"""
        results_text = "\n\n".join(
            f"## 步骤 {r['step_id']}: {r['task']}\n{r['result']}"
            for r in results
        )

        # 如果所有步骤结果都很短，直接返回
        all_short = all(len(r['result']) < 100 for r in results)
        if all_short and len(results) <= 2:
            return f"## 执行计划\n{analysis}\n\n## 执行结果\n{results_text}"

        messages = [
            {"role": "system", "content": "你是一个任务汇总专家。请根据各步骤的执行结果，给出最终的完整回答。整合所有步骤的输出，形成连贯的结论。用中文回答。"},
            {"role": "user", "content": f"原始任务: {user_input}\n\n任务分析: {analysis}\n\n各步骤执行结果:\n{results_text}"},
        ]

        try:
            for model_id in self.model_priority:
                try:
                    result = chat_completion(model_id, messages, max_tokens=4096, temperature=0.5)
                    if result and result.strip():
                        return result
                except Exception:
                    continue
            # LLM 全部失败，返回原始结果
            return f"## 执行计划\n{analysis}\n\n## 执行结果\n{results_text}"
        except Exception:
            return f"## 执行计划\n{analysis}\n\n## 执行结果\n{results_text}"


class PlanReflectionEngine:
    """
    Plan + Reflection 组合引擎。

    策略：用 Plan-Execute 做规划和执行，最后用 Reflection 审查和修正输出质量。
    适合需要高质量最终输出的任务。
    """

    def __init__(
        self,
        model_priority: list[str],
        *,
        max_steps: int = 15,
        review_rounds: int = 2,
        pass_threshold: int = 7,
    ) -> None:
        self.model_priority = model_priority
        self.max_steps = max_steps
        self.review_rounds = review_rounds
        self.pass_threshold = pass_threshold
        self.planner = PlanExecuteEngine(model_priority, max_steps=max_steps)
        self.reflector = ReflectionEngine(
            model_priority,
            max_rounds=review_rounds,
            pass_threshold=pass_threshold,
        )

    def run(self, user_input: str, context: AgentContext | None = None) -> str:
        ctx = context or AgentContext()

        # Phase 1: Plan-Execute 执行
        logger.info("PlanReflection Phase 1: 规划并执行")
        initial_output = self.planner.run(user_input, context=ctx)

        # Phase 2: Reflection 审查和修正
        logger.info("PlanReflection Phase 2: 反思审查")
        try:
            final_output = self.reflector.run(
                f"原始任务: {user_input}\n\n执行结果:\n{initial_output}", context=ctx
            )
        except Exception as e:
            logger.warning(f"Reflection 阶段失败: {e}")
            final_output = initial_output

        return final_output


class ReactReflectionEngine:
    """
    ReAct + Reflection 组合引擎。

    策略：用 ReAct 进行探索和执行，最后用 Reflection 审查输出质量。
    适合需要工具探索且要求高质量输出的任务。
    """

    def __init__(
        self,
        model_priority: list[str],
        *,
        react_iterations: int = 8,
        review_rounds: int = 2,
        pass_threshold: int = 7,
    ) -> None:
        self.model_priority = model_priority
        self.react_iterations = react_iterations
        self.review_rounds = review_rounds
        self.pass_threshold = pass_threshold
        self.reactor = ReActEngine(model_priority, max_iterations=react_iterations)
        self.reflector = ReflectionEngine(
            model_priority,
            max_rounds=review_rounds,
            pass_threshold=pass_threshold,
        )

    def run(self, user_input: str, context: AgentContext | None = None) -> str:
        ctx = context or AgentContext()

        # Phase 1: ReAct 探索和执行
        logger.info("ReactReflection Phase 1: ReAct 探索执行")
        initial_output = self.reactor.run(user_input, context=ctx)

        # Phase 2: Reflection 审查和修正
        logger.info("ReactReflection Phase 2: 反思审查")
        try:
            final_output = self.reflector.run(
                f"原始任务: {user_input}\n\n执行结果:\n{initial_output}", context=ctx
            )
        except Exception as e:
            logger.warning(f"Reflection 阶段失败: {e}")
            final_output = initial_output

        return final_output
