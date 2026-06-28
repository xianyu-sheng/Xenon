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

from omniagent.engine.base_engine import BaseEngine
from omniagent.engine.callbacks import EngineCallback
from omniagent.engine.context import AgentContext
from omniagent.engine.plan_execute_engine import PlanExecuteEngine
from omniagent.engine.react_engine import ReActEngine, BUILTIN_TOOLS
from omniagent.engine.reflection_engine import ReflectionEngine
from omniagent.utils.llm_client import chat_completion

logger = logging.getLogger(__name__)


class PlanReactEngine(BaseEngine):
    """
    Plan + React 组合引擎。

    策略：用 Plan-Execute 做全局规划，每个步骤用 ReAct 循环执行。
    适合需要既有宏观规划、又有灵活工具调用的复杂任务。
    """

    def __init__(
        self,
        model_priority: list[str],
        *,
        executor_model_priority: list[str] | None = None,
        max_steps: int = 15,
        react_iterations: int = 8,
        callback: EngineCallback | None = None,
    ) -> None:
        super().__init__(model_priority=model_priority, callback=callback)
        executor = executor_model_priority or model_priority
        self.max_steps = max_steps
        self.react_iterations = react_iterations
        self.planner = PlanExecuteEngine(
            model_priority, executor_model_priority=executor,
            max_steps=max_steps, callback=self.callback,
        )
        self.reactor = ReActEngine(executor, max_iterations=react_iterations, callback=self.callback)

    def run(self, user_input: str, context: AgentContext | None = None) -> str:
        from rich.console import Console
        console = Console()

        ctx = context or AgentContext()

        # Phase 0: Scout — 使用统一的 DirectoryScout 服务
        from omniagent.engine.directory_scout import DirectoryScout
        scout = DirectoryScout()
        scout_result = scout.scout(user_input, ctx, None)
        if not scout_result.has_data:
            scout_result = scout.scout_from_history(user_input, ctx, None)

        if scout_result and scout_result.has_data:
            plan_input = scout_result.to_plan_input(user_input)
        else:
            plan_input = (
                f"{user_input}\n\n"
                "## 🔴 重要：当前消息中没有指定目录路径，且未获取到文件列表。\n"
                "如果你的任务需要访问本地文件，第一步必须是 list_files。\n"
                "**绝对禁止**猜测不存在的文件名或目录名。\n"
                "如果你不需要访问文件（如纯对话/解释/展开已有分析），所有步骤的 tool 设为 null。"
            )

        # Phase 1: 全局规划（现在有真实文件列表）
        console.print("[dim]📋 Phase 1: 生成执行计划...[/dim]")
        try:
            plan = self.planner._plan(plan_input, ctx)
        except Exception as e:
            console.print(f"[red]  ✗ 规划阶段异常: {e}[/red]")
            return f"规划阶段失败: {e}"
        steps = plan.get("steps", [])
        analysis = plan.get("analysis", "")
        logger.debug(f"Plan 结果: steps={len(steps)}, analysis={analysis[:200] if analysis else '(空)'}")

        if not steps:
            console.print(f"[yellow]  ⚠ 未生成步骤，analysis={analysis[:200] if analysis else '(空)'}[/yellow]")
            return analysis or "未能生成有效的执行计划。"

        console.print(f"[dim]📋 计划生成 {len(steps)} 个步骤[/dim]")

        # 显示计划
        for s in steps:
            console.print(f"  [dim]步骤 {s.get('id', '?')}: {s.get('task', '')}[/dim]")

        # Phase 2: 每步用 ReAct 执行（支持 DAG 并行）
        has_deps = any(
            isinstance(s, dict) and s.get("depends_on") and len(s.get("depends_on", [])) > 0
            for s in steps
        )

        if has_deps:
            # ── DAG 并行路径 ──
            console.print(f"\n[dim]🔄 Phase 2: DAG 并行执行[/dim]")
            try:
                from omniagent.engine.plan_dag import PlanDAG, DAGExecutor
                from omniagent.repl.cards import PlanProgressCard

                dag = PlanDAG.from_plan(plan)
                errors = dag.validate()
                if errors:
                    logger.warning("Plan DAG 验证失败: %s，回退串行", errors)
                    console.print(f"[yellow]  ⚠ DAG 验证警告: {'; '.join(errors)}[/yellow]")
                    # 继续使用 DAG — 验证错误通常是信息性的
                    # （如 LLM 引用了不存在的步骤 ID），不影响串行回退行为

                logger.info(
                    "DAG: %d steps, %d waves, has_parallelism=%s",
                    dag.total_steps, dag.wave_count, dag.has_parallelism,
                )
                if dag.has_parallelism:
                    console.print(
                        f"[dim]  🎯 {dag.total_steps} 步, {dag.wave_count} 波 "
                        f"(可并行)[/dim]"
                    )
                else:
                    console.print(
                        f"[dim]  🎯 {dag.total_steps} 步, 串行执行[/dim]"
                    )

                progress_card = PlanProgressCard(
                    [s for s in dag.steps.values()], title="执行计划"
                )

                executor = DAGExecutor(
                    model_priority=self.model_priority,
                    callback=self.callback,
                    react_iterations=self.react_iterations,
                )

                from rich.live import Live
                with Live(progress_card, console=console, refresh_per_second=8, transient=False):
                    summary = executor.run(
                        dag, user_input, ctx,
                        analysis=analysis, progress_card=progress_card,
                    )

                console.print("[dim]📝 汇总结果...[/dim]")
                return summary
            except Exception as e:
                logger.warning("DAG 执行失败: %s，回退串行", e)
                console.print(f"[yellow]  ⚠ DAG 并行失败: {e}，回退串行执行[/yellow]")
                # 回退到下面的串行路径

        # ── 串行路径（回退或默认）──
        console.print(f"\n[dim]🔄 Phase 2: ReAct 逐步执行[/dim]")
        results = []

        # 收集所有已发现的信息（文件列表、命令输出等）
        discovered_info: list[str] = []

        for i, step in enumerate(steps[:self.max_steps]):
            step_task = step.get("task", "")
            step_id = step.get("id", i + 1)

            self.callback.on_step(step_id, len(steps), step_task)
            console.print(f"\n[cyan]🔄 步骤 {step_id}/{len(steps)}: {step_task}[/cyan]")

            # 构建包含全局上下文的 ReAct 输入
            prev_context = ""
            if results:
                prev_context = "\n\n## 之前步骤已发现的信息:\n" + "\n".join(
                    f"- 步骤 {r['step_id']} ({r['task']}): {r['result'][:300]}"
                    for r in results
                )

            # 注入已发现的关键信息
            info_context = ""
            if discovered_info:
                info_context = "\n\n## 已知信息（不要重复查询）:\n" + "\n".join(
                    f"- {info}" for info in discovered_info[-20:]
                )

            react_input = (
                f"全局任务: {user_input}\n"
                f"当前步骤 ({step_id}/{len(steps)}): {step_task}"
                f"{prev_context}{info_context}\n\n"
                f"重要：利用上面已有的信息，不要重复执行已经完成的操作。"
            )

            # 用 ReAct 执行当前步骤
            try:
                step_result = self.reactor.run(react_input, context=ctx)
                if not step_result or not step_result.strip():
                    step_result = f"(步骤 {step_id} 执行完成，无文本输出)"
                console.print(f"[green]  ✓ 步骤 {step_id} 完成 ({len(step_result)} 字符)[/green]")

                # 从结果中提取关键信息（文件路径、命令输出等）
                import re
                # 提取文件路径
                paths = re.findall(r'[\w/\\.-]+\.(?:py|js|ts|json|yaml|yml|toml|md|txt|bat|ps1|png|ico)', step_result)
                for p in paths[:5]:
                    discovered_info.append(f"文件: {p}")
                # 提取关键发现
                if "Windows" in step_result:
                    discovered_info.append("操作系统: Windows")
                if "Linux" in step_result:
                    discovered_info.append("操作系统: Linux")

                self.callback.on_step_done(step_id, True, step_result[:200])

            except Exception as e:
                step_result = f"步骤执行失败: {e}"
                console.print(f"[red]  ✗ 步骤 {step_id} 失败: {e}[/red]")
                self.callback.on_step_done(step_id, False, step_result[:200])

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
            result = self._call_llm(messages, max_tokens=4096, temperature=0.5)
            if result and result.strip():
                return result
            # LLM 全部失败，返回原始结果
            return f"## 执行计划\n{analysis}\n\n## 执行结果\n{results_text}"
        except Exception:
            return f"## 执行计划\n{analysis}\n\n## 执行结果\n{results_text}"


class PlanReflectionEngine(BaseEngine):
    """
    Plan + Reflection 组合引擎。

    策略：用 Plan-Execute 做规划和执行，最后用 Reflection 审查和修正输出质量。
    适合需要高质量最终输出的任务。
    """

    def __init__(
        self,
        model_priority: list[str],
        *,
        executor_model_priority: list[str] | None = None,
        reviewer_model_priority: list[str] | None = None,
        max_steps: int = 15,
        review_rounds: int = 2,
        pass_threshold: int = 7,
        callback: EngineCallback | None = None,
    ) -> None:
        super().__init__(model_priority=model_priority, callback=callback)
        executor = executor_model_priority or model_priority
        reviewer = reviewer_model_priority or model_priority
        self.max_steps = max_steps
        self.review_rounds = review_rounds
        self.pass_threshold = pass_threshold
        self.planner = PlanExecuteEngine(
            model_priority, executor_model_priority=executor,
            max_steps=max_steps, callback=self.callback,
        )
        self.reflector = ReflectionEngine(
            model_priority,
            executor_model_priority=executor,
            reviewer_model_priority=reviewer,
            max_rounds=review_rounds,
            pass_threshold=pass_threshold,
            callback=self.callback,
        )

    def run(self, user_input: str, context: AgentContext | None = None) -> str:
        ctx = context or AgentContext()

        # Phase 1: Plan-Execute 执行
        logger.debug("PlanReflection Phase 1: 规划并执行")
        initial_output = self.planner.run(user_input, context=ctx)

        # Phase 2: Reflection 审查和修正
        logger.debug("PlanReflection Phase 2: 反思审查")
        try:
            final_output = self.reflector.run(
                f"原始任务: {user_input}\n\n执行结果:\n{initial_output}", context=ctx
            )
        except Exception as e:
            logger.warning(f"Reflection 阶段失败: {e}", exc_info=True)
            self.callback.on_error(f"Reflection 阶段失败: {e}")
            final_output = (
                f"{initial_output}\n\n"
                f"---\n"
                f"## ⚠️ Reflection 审查阶段失败\n\n"
                f"原因: {e}\n"
                f"以上为前一阶段的原始输出，未经审查修正。"
            )

        return final_output


class ReactReflectionEngine(BaseEngine):
    """
    ReAct + Reflection 组合引擎。

    策略：用 ReAct 进行探索和执行，最后用 Reflection 审查输出质量。
    适合需要工具探索且要求高质量输出的任务。
    """

    def __init__(
        self,
        model_priority: list[str],
        *,
        executor_model_priority: list[str] | None = None,
        reviewer_model_priority: list[str] | None = None,
        react_iterations: int = 8,
        review_rounds: int = 2,
        pass_threshold: int = 7,
        callback: EngineCallback | None = None,
    ) -> None:
        super().__init__(model_priority=model_priority, callback=callback)
        executor = executor_model_priority or model_priority
        reviewer = reviewer_model_priority or model_priority
        self.react_iterations = react_iterations
        self.review_rounds = review_rounds
        self.pass_threshold = pass_threshold
        self.reactor = ReActEngine(executor, max_iterations=react_iterations, callback=self.callback)
        self.reflector = ReflectionEngine(
            model_priority,
            executor_model_priority=executor,
            reviewer_model_priority=reviewer,
            max_rounds=review_rounds,
            pass_threshold=pass_threshold,
            callback=self.callback,
        )

    def run(self, user_input: str, context: AgentContext | None = None) -> str:
        ctx = context or AgentContext()

        # Phase 1: ReAct 探索和执行
        logger.debug("ReactReflection Phase 1: ReAct 探索执行")
        initial_output = self.reactor.run(user_input, context=ctx)

        # Phase 2: Reflection 审查和修正
        logger.debug("ReactReflection Phase 2: 反思审查")
        try:
            final_output = self.reflector.run(
                f"原始任务: {user_input}\n\n执行结果:\n{initial_output}", context=ctx
            )
        except Exception as e:
            logger.warning(f"Reflection 阶段失败: {e}", exc_info=True)
            self.callback.on_error(f"Reflection 阶段失败: {e}")
            final_output = (
                f"{initial_output}\n\n"
                f"---\n"
                f"## ⚠️ Reflection 审查阶段失败\n\n"
                f"原因: {e}\n"
                f"以上为前一阶段的原始输出，未经审查修正。"
            )

        return final_output
