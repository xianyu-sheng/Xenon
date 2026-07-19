"""
Combined Engines — 思考范式组合引擎。

将多种思考范式组合使用，发挥各自优势：
- PlanReactEngine: 全局规划 + 每步 ReAct 执行
- PlanReflectionEngine: 规划执行 + 反思修正
- ReactReflectionEngine: ReAct 探索 + 反思审查
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from xenon.engine.callbacks import EngineCallback
from xenon.engine.context import AgentContext
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.react_engine import ReActEngine, BUILTIN_TOOLS
from xenon.engine.reflection_engine import ReflectionEngine
from xenon.utils.llm_client import chat_completion

if TYPE_CHECKING:
    from xenon.repl.context_manager import ContextManager

logger = logging.getLogger(__name__)


def _isolated_ctx(ctx: AgentContext) -> AgentContext:
    """为 reflector 构造隔离 ctx（P3-Q9 / §8.19.6）。

    新 store 不含 reactor 写入的 ``step_N_result`` 等中间状态，避免反思基线被
    污染；仅复制对话消息作历史兜底（ctx_mgr 注入时 reflector 走 ctx_mgr）。
    """
    fresh = AgentContext()
    fresh.set_conversation_messages(list(ctx.get_conversation_messages()))
    return fresh


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
        react_iterations: int = 8,
        callback: EngineCallback | None = None,
        model_configs: dict[str, Any] | None = None,
        model_pool: Any = None,          # v0.4.0
        auto_router: Any = None,         # v0.4.0 Step 13
        permission_gate: Any = None,     # v0.5.0
    ) -> None:
        self.model_priority = model_priority
        self.max_steps = max_steps
        self.react_iterations = react_iterations
        self.callback = callback or EngineCallback()
        self.model_pool = model_pool
        self.auto_router = auto_router
        self.planner = PlanExecuteEngine(model_priority, max_steps=max_steps, callback=self.callback, model_configs=model_configs, model_pool=model_pool, auto_router=auto_router, permission_gate=permission_gate)
        self.reactor = ReActEngine(model_priority, max_iterations=react_iterations, callback=self.callback, model_configs=model_configs, model_pool=model_pool, auto_router=auto_router, permission_gate=permission_gate)

    def run(
        self,
        user_input: str,
        context: AgentContext | None = None,
        ctx_mgr: ContextManager | None = None,
    ) -> str:
        from rich.console import Console
        console = Console()

        ctx = context or AgentContext()
        # F4: 把 ctx_mgr 透传给子引擎（_plan 经 _ctx_mgr 消费，reactor 经 run ctx_mgr）
        self.planner._ctx_mgr = ctx_mgr

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

        # 收集所有已发现的信息（文件列表、命令输出等）
        discovered_info: list[str] = []

        for i, step in enumerate(steps[:self.max_steps]):
            step_task = step.get("task", "")
            step_id = step.get("id", i + 1)

            console.print(f"\n[cyan]🔄 步骤 {step_id}/{len(steps)}: {step_task}[/cyan]")

            # 构建包含全局上下文的 ReAct 输入
            # P3-Q9 / §8.19.1：prev_context 只含成功步骤，失败错误串不当"已发现信息"
            prev_context = ""
            ok_results = [r for r in results if r.get("status") == "ok"]
            if ok_results:
                prev_context = "\n\n## 之前步骤已发现的信息:\n" + "\n".join(
                    f"- 步骤 {r['step_id']} ({r['task']}): {r['result'][:300]}"
                    for r in ok_results
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
            # P3-Q9 / §8.19.1：标记步骤成败，失败错误串不进入 discovered_info/prev_context
            status = "ok"
            try:
                step_result = self.reactor.run(react_input, context=ctx, ctx_mgr=ctx_mgr)
                if not step_result or not step_result.strip():
                    step_result = f"(步骤 {step_id} 执行完成，无文本输出)"
                console.print(f"[green]  ✓ 步骤 {step_id} 完成 ({len(step_result)} 字符)[/green]")

                # 仅从成功结果提取关键信息（文件路径、命令输出等）
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

            except Exception as e:
                status = "failed"
                step_result = f"步骤执行失败: {e}"
                console.print(f"[red]  ✗ 步骤 {step_id} 失败: {e}[/red]")

            results.append({
                "step_id": step_id,
                "task": step_task,
                "result": step_result,
                "status": status,
            })

            # 存入上下文（标记状态，供跨引擎消费方区分成功/失败）
            ctx.set(f"step_{step_id}_result", step_result)
            ctx.set(f"step_{step_id}_status", status)

        # Phase 3: 汇总
        console.print(f"\n[dim]📝 Phase 3: 汇总结果...[/dim]")
        summary = self._summarize(user_input, results, analysis)
        return summary

    def _summarize(self, user_input: str, results: list[dict], analysis: str = "") -> str:
        """汇总所有步骤的结果。

        P3-Q9 / §8.19.1：区分成功/失败步骤——失败步骤单列"失败的步骤"段，
        不与成功结果混排，避免错误串被当正常结果整合。
        """
        ok_results = [r for r in results if r.get("status") == "ok"]
        failed_results = [r for r in results if r.get("status") == "failed"]

        results_text = "\n\n".join(
            f"## 步骤 {r['step_id']}: {r['task']}\n{r['result']}"
            for r in ok_results
        ) if ok_results else "(无成功完成的步骤)"

        failed_text = ""
        if failed_results:
            failed_text = "\n\n## 失败的步骤\n" + "\n".join(
                f"- 步骤 {r['step_id']} ({r['task']}): {r['result']}"
                for r in failed_results
            )

        # 如果所有成功步骤结果都很短，直接返回
        all_short = all(len(r['result']) < 100 for r in ok_results)
        if all_short and len(ok_results) <= 2:
            return f"## 执行计划\n{analysis}\n\n## 执行结果\n{results_text}{failed_text}"

        messages = [
            {"role": "system", "content": "你是一个任务汇总专家。请根据各步骤的执行结果，给出最终的完整回答。整合所有步骤的输出，形成连贯的结论。用中文回答。"},
            {"role": "user", "content": f"原始任务: {user_input}\n\n任务分析: {analysis}\n\n各步骤执行结果:\n{results_text}{failed_text}"},
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
            return f"## 执行计划\n{analysis}\n\n## 执行结果\n{results_text}{failed_text}"
        except Exception:
            return f"## 执行计划\n{analysis}\n\n## 执行结果\n{results_text}{failed_text}"


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
        callback: EngineCallback | None = None,
        model_configs: dict[str, Any] | None = None,
        model_pool: Any = None,          # v0.4.0
        auto_router: Any = None,         # v0.4.0 Step 13
        permission_gate: Any = None,     # v0.5.0
    ) -> None:
        self.model_priority = model_priority
        self.max_steps = max_steps
        self.review_rounds = review_rounds
        self.pass_threshold = pass_threshold
        self.callback = callback or EngineCallback()
        self.model_pool = model_pool
        self.auto_router = auto_router
        self.planner = PlanExecuteEngine(model_priority, max_steps=max_steps, callback=self.callback, model_configs=model_configs, model_pool=model_pool, auto_router=auto_router, permission_gate=permission_gate)
        self.reflector = ReflectionEngine(
            model_priority,
            max_rounds=review_rounds,
            pass_threshold=pass_threshold,
            callback=self.callback,
            model_configs=model_configs,
            model_pool=model_pool,
            auto_router=auto_router,
            permission_gate=permission_gate,
        )

    def run(
        self,
        user_input: str,
        context: AgentContext | None = None,
        ctx_mgr: ContextManager | None = None,
    ) -> str:
        ctx = context or AgentContext()

        # Phase 1: Plan-Execute 执行
        logger.info("PlanReflection Phase 1: 规划并执行")
        initial_output = self.planner.run(user_input, context=ctx, ctx_mgr=ctx_mgr)

        # Phase 2: Reflection 审查和修正
        logger.info("PlanReflection Phase 2: 反思审查")
        # P3-Q9 / §8.19.6：reflector 用隔离 ctx，避免 reactor 中间状态污染反思基线
        reflector_ctx = _isolated_ctx(ctx)
        try:
            final_output = self.reflector.run(
                f"原始任务: {user_input}\n\n执行结果:\n{initial_output}",
                context=reflector_ctx, ctx_mgr=ctx_mgr,
            )
        except Exception as e:
            logger.warning(f"Reflection 阶段失败: {e}")
            self.callback.on_error(f"Reflection 阶段失败: {e}")
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
        callback: EngineCallback | None = None,
        model_configs: dict[str, Any] | None = None,
        model_pool: Any = None,          # v0.4.0
        auto_router: Any = None,         # v0.4.0 Step 13
        permission_gate: Any = None,     # v0.5.0
    ) -> None:
        self.model_priority = model_priority
        self.react_iterations = react_iterations
        self.review_rounds = review_rounds
        self.pass_threshold = pass_threshold
        self.callback = callback or EngineCallback()
        self.model_pool = model_pool
        self.auto_router = auto_router
        self.reactor = ReActEngine(model_priority, max_iterations=react_iterations, callback=self.callback, model_configs=model_configs, model_pool=model_pool, auto_router=auto_router, permission_gate=permission_gate)
        self.reflector = ReflectionEngine(
            model_priority,
            max_rounds=review_rounds,
            pass_threshold=pass_threshold,
            callback=self.callback,
            model_configs=model_configs,
            model_pool=model_pool,
            auto_router=auto_router,
            permission_gate=permission_gate,
        )

    def run(
        self,
        user_input: str,
        context: AgentContext | None = None,
        ctx_mgr: ContextManager | None = None,
    ) -> str:
        ctx = context or AgentContext()

        # Phase 1: ReAct 探索和执行
        logger.info("ReactReflection Phase 1: ReAct 探索执行")
        initial_output = self.reactor.run(user_input, context=ctx, ctx_mgr=ctx_mgr)

        # Phase 2: Reflection 审查和修正
        logger.info("ReactReflection Phase 2: 反思审查")
        # P3-Q9 / §8.19.6：reflector 用隔离 ctx，避免 reactor 中间状态污染反思基线
        reflector_ctx = _isolated_ctx(ctx)
        try:
            final_output = self.reflector.run(
                f"原始任务: {user_input}\n\n执行结果:\n{initial_output}",
                context=reflector_ctx, ctx_mgr=ctx_mgr,
            )
        except Exception as e:
            logger.warning(f"Reflection 阶段失败: {e}")
            self.callback.on_error(f"Reflection 阶段失败: {e}")
            final_output = initial_output

        return final_output
