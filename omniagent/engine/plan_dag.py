"""Plan DAG — 带依赖关系的计划步骤 + 拓扑排序 + 并行执行。

将 LLM 生成的线性步骤列表（含 depends_on 标注）构建为 DAG，
通过拓扑排序计算并行波次（wave），每波内步骤可安全并行执行。

核心设计：
- 步骤无 depends_on → 退化为串行（每波 1 步），行为与旧版完全一致
- LLM 标注 depends_on → 自动并行化独立步骤
- asyncio.to_thread() 桥接同步 ReActEngine，复用现有 subagent 并发模式
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from omniagent.engine.context import AgentContext

logger = logging.getLogger(__name__)


# ── 数据结构 ────────────────────────────────────────────────────


@dataclass
class PlanStep:
    """带依赖关系的计划步骤。

    从 LLM 生成的 JSON plan 步骤转换而来，
    增加了 status / result / duration_ms 等运行时字段。
    """

    id: int
    task: str
    tool: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: list[int] = field(default_factory=list)
    # 运行时字段
    status: str = "pending"  # pending | running | done | failed
    result: str = ""
    duration_ms: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanStep:
        """从 LLM 输出的步骤字典构造 PlanStep。

        自动规范化 depends_on：字符串 → 整数，非列表 → 空列表。
        """
        step_id = int(data.get("id", 0))

        # 规范化 depends_on
        raw_deps = data.get("depends_on", [])
        if not isinstance(raw_deps, list):
            raw_deps = [raw_deps] if raw_deps else []
        depends_on = [int(d) for d in raw_deps]

        return cls(
            id=step_id,
            task=str(data.get("task", "")),
            tool=data.get("tool") if data.get("tool") and data.get("tool") != "null" else None,
            params=data.get("params", {}) or {},
            depends_on=depends_on,
        )

    @property
    def is_tool_step(self) -> bool:
        """是否需要工具执行。"""
        return self.tool is not None

    @property
    def waiting_for(self) -> list[int]:
        """返回该步骤仍在等待的依赖步骤 ID 列表（供 UI 显示）。"""
        return self.depends_on


# ── DAG 构建器 ──────────────────────────────────────────────────


class PlanDAG:
    """从带 depends_on 标注的步骤列表构建 DAG，计算并行波次。

    使用 Kahn's algorithm 进行拓扑排序：
    - 计算每个节点的入度（未满足的依赖数）
    - 入度为 0 的节点进入当前波次
    - 执行完一波后，减少被依赖节点的入度
    - 重复直到所有节点处理完毕

    若所有步骤的 depends_on 均为空 → 退化为串行（每波 1 步）。
    """

    def __init__(self, steps: list[PlanStep]) -> None:
        if not steps:
            raise ValueError("步骤列表不能为空")

        self.steps: dict[int, PlanStep] = {}
        for s in steps:
            self.steps[s.id] = s

        self._waves: list[list[PlanStep]] | None = None

    # ── 工厂方法 ─────────────────────────────────────────────

    @classmethod
    def from_plan(cls, plan: dict[str, Any]) -> PlanDAG:
        """从 parse_plan() 输出的 dict 构建 PlanDAG。

        Args:
            plan: parse_plan() 返回的字典，包含 steps 列表

        Returns:
            PlanDAG 实例
        """
        raw_steps = plan.get("steps", [])
        if not raw_steps:
            raise ValueError("plan 中没有 steps")

        steps = [PlanStep.from_dict(s) for s in raw_steps]
        return cls(steps)

    # ── 验证 ─────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """验证 DAG 的合法性。返回错误信息列表，空列表表示合法。

        检查项：
        - 循环依赖检测
        - 无效依赖引用（depends_on 指向不存在的步骤 ID）
        - 自依赖检测
        """
        errors: list[str] = []

        for sid, step in self.steps.items():
            for dep_id in step.depends_on:
                if dep_id == sid:
                    errors.append(f"步骤 {sid} 依赖自身")
                elif dep_id not in self.steps:
                    errors.append(
                        f"步骤 {sid} 依赖不存在的步骤 {dep_id}（可用: {sorted(self.steps.keys())}）"
                    )

        # 循环依赖检测：尝试拓扑排序，若无法完成则存在环
        if not errors:
            try:
                self._compute_waves()
            except ValueError as e:
                errors.append(str(e))

        return errors

    # ── 波次计算 ─────────────────────────────────────────────

    def waves(self) -> list[list[PlanStep]]:
        """返回拓扑排序后的并行波次列表。

        每波包含所有依赖已满足的步骤，波内步骤可安全并行执行。

        Returns:
            [[wave_0_steps], [wave_1_steps], ...]

        Raises:
            ValueError: 若存在循环依赖
        """
        if self._waves is None:
            self._waves = self._compute_waves()
        return self._waves

    def _compute_waves(self) -> list[list[PlanStep]]:
        """Kahn's algorithm 拓扑排序，按层分组。"""
        # 入度 = 该步骤有多少个依赖尚未满足
        in_degree: dict[int, int] = {}
        # 反向依赖：step_id → 依赖它的步骤列表
        reverse_deps: dict[int, list[int]] = {sid: [] for sid in self.steps}

        for sid, step in self.steps.items():
            resolved_deps = [d for d in step.depends_on if d in self.steps]
            in_degree[sid] = len(resolved_deps)
            for dep_id in resolved_deps:
                reverse_deps[dep_id].append(sid)

        # 入度为 0 的步骤作为起始波次
        queue: deque[int] = deque(
            sid for sid, deg in in_degree.items() if deg == 0
        )

        if not queue:
            # 所有步骤都有依赖但无起始节点 → 循环依赖
            raise ValueError(
                f"检测到循环依赖：所有 {len(self.steps)} 个步骤都有未满足的依赖"
            )

        waves: list[list[PlanStep]] = []
        processed_count = 0

        while queue:
            # 当前波次：所有入度为 0 的节点
            wave_size = len(queue)
            current_wave: list[PlanStep] = []

            for _ in range(wave_size):
                sid = queue.popleft()
                current_wave.append(self.steps[sid])
                processed_count += 1

                # 减少被依赖节点的入度
                for dependent_id in reverse_deps[sid]:
                    in_degree[dependent_id] -= 1
                    if in_degree[dependent_id] == 0:
                        queue.append(dependent_id)

            waves.append(current_wave)

        if processed_count != len(self.steps):
            # 有节点未被处理 → 循环依赖
            unprocessed = sorted(set(self.steps.keys()) - set(
                sid for wave in waves for sid in (s.id for s in wave)
            ))
            raise ValueError(
                f"检测到循环依赖：步骤 {unprocessed} 无法被调度（存在环）"
            )

        return waves

    # ── 查询 ─────────────────────────────────────────────────

    @property
    def has_parallelism(self) -> bool:
        """是否有可并行的步骤（至少有一波包含 2+ 步骤）。"""
        return any(len(w) > 1 for w in self.waves())

    @property
    def total_steps(self) -> int:
        return len(self.steps)

    @property
    def wave_count(self) -> int:
        return len(self.waves())

    def completed_count(self) -> int:
        """返回已完成的步骤数。"""
        return sum(1 for s in self.steps.values() if s.status == "done")

    def failed_count(self) -> int:
        """返回失败的步骤数。"""
        return sum(1 for s in self.steps.values() if s.status == "failed")


# ── 并行执行器 ──────────────────────────────────────────────────


class DAGExecutor:
    """按 DAG 波次并行执行计划步骤。

    每波内的步骤通过 asyncio.gather + asyncio.to_thread 并行执行，
    波与波之间串行（下一波依赖上一波的结果）。

    使用 asyncio.Semaphore 限制最大并发数，避免 LLM API 过载。
    """

    def __init__(
        self,
        model_priority: list[str],
        *,
        callback: Any = None,  # EngineCallback
        max_concurrent: int = 5,
        react_iterations: int = 8,
        tracker: Any = None,  # ToolExecutionTracker
    ) -> None:
        self.model_priority = model_priority
        self.callback = callback
        self.max_concurrent = max_concurrent
        self.react_iterations = react_iterations
        self.tracker = tracker
        self._semaphore = asyncio.Semaphore(max_concurrent)

    def run(
        self,
        dag: PlanDAG,
        user_input: str,
        context: AgentContext | None = None,
        *,
        analysis: str = "",
        progress_card: Any | None = None,  # PlanProgressCard
    ) -> str:
        """同步入口：asyncio.run() 包装，逐波执行。

        Args:
            dag: 已构建的 PlanDAG
            user_input: 原始用户任务
            context: 共享上下文
            analysis: 规划阶段的任务分析（传入汇总 LLM）
            progress_card: 可选的进度卡片（用于 Live 渲染）

        Returns:
            汇总后的最终结果文本
        """
        return asyncio.run(
            self._run_async(dag, user_input, context, analysis, progress_card)
        )

    async def _run_async(
        self,
        dag: PlanDAG,
        user_input: str,
        context: AgentContext | None = None,
        analysis: str = "",
        progress_card: Any | None = None,
    ) -> str:
        """异步执行所有波次。"""
        ctx = context or AgentContext()
        discovered_info: list[str] = []
        all_results: list[dict[str, Any]] = []

        waves = dag.waves()
        logger.info(
            "DAG 执行开始: %d 步, %d 波 (%d 个并行机会)",
            dag.total_steps, len(waves),
            sum(1 for w in waves if len(w) > 1),
        )

        for wave_idx, wave in enumerate(waves):
            wave_label = (
                f"Wave {wave_idx + 1}/{len(waves)}"
                if len(waves) > 1
                else f"步骤 {wave[0].id}"
            )
            logger.debug(
                "%s: %d 个步骤 [%s]",
                wave_label,
                len(wave),
                ", ".join(f"#{s.id} {s.task[:30]}" for s in wave),
            )

            # 构建前序结果上下文
            prev_context = ""
            if all_results:
                prev_context = "\n\n## 之前步骤的结果:\n" + "\n".join(
                    f"- 步骤 {r['step_id']} ({r['task']}): {r['result'][:300]}"
                    for r in all_results
                )

            info_context = ""
            if discovered_info:
                info_context = "\n\n## 已知信息（不要重复查询）:\n" + "\n".join(
                    f"- {info}" for info in discovered_info[-20:]
                )

            # 并行执行波内步骤
            if len(wave) == 1:
                # 单步骤 → 直接同步执行，避免线程开销
                result = await self._execute_single_step(
                    wave[0], user_input, prev_context, info_context, ctx
                )
                wave_results = [result]
            else:
                # 多步骤 → 并行执行
                tasks = []
                for step in wave:
                    tasks.append(
                        self._execute_single_step(
                            step, user_input, prev_context, info_context, ctx
                        )
                    )
                wave_results = await asyncio.gather(*tasks)

            # 处理波次结果
            for step, result_dict in zip(wave, wave_results):
                if result_dict is None:
                    step.status = "failed"
                    step.result = "步骤执行异常（返回空）"
                else:
                    step.status = "done" if result_dict.get("success", True) else "failed"
                    step.result = result_dict.get("result", "")
                    step.duration_ms = result_dict.get("duration_ms", 0.0)

                all_results.append({
                    "step_id": step.id,
                    "task": step.task,
                    "result": step.result,
                })

                # 提取发现的信息供后续波次使用
                self._extract_discoveries(step, discovered_info)

                # 更新进度卡片
                if progress_card:
                    progress_card.update(
                        step.id, step.status, step.result, step.duration_ms
                    )

                # 通知回调
                if self.callback:
                    try:
                        self.callback.on_step_done(
                            step.id,
                            step.status == "done",
                            step.result[:200],
                        )
                    except Exception:
                        pass

            logger.debug(
                "%s 完成: %d 成功, %d 失败",
                wave_label,
                sum(1 for r in wave_results if r and r.get("success", True)),
                sum(1 for r in wave_results if r and not r.get("success", True)),
            )

        # 汇总所有结果
        return self._summarize(user_input, all_results, analysis=analysis)

    async def _execute_single_step(
        self,
        step: PlanStep,
        user_input: str,
        prev_context: str,
        info_context: str,
        context: AgentContext,
    ) -> dict[str, Any] | None:
        """执行单个步骤（可能是工具步骤或 LLM 步骤）。

        工具步骤：直接通过 ToolExecutor 执行
        LLM 步骤：通过 ReActEngine 的 mini loop 执行
        """
        async with self._semaphore:
            start_time = time.monotonic()

            try:
                if step.is_tool_step:
                    result_text = await asyncio.to_thread(
                        self._run_tool_step, step, context
                    )
                else:
                    react_input = (
                        f"全局任务: {user_input}\n"
                        f"当前步骤: {step.task}"
                        f"{prev_context}{info_context}\n\n"
                        f"重要：利用上面已有的信息，不要重复执行已经完成的操作。"
                    )
                    result_text = await asyncio.to_thread(
                        self._run_llm_step, react_input, context
                    )

                duration_ms = (time.monotonic() - start_time) * 1000
                return {
                    "success": True,
                    "result": result_text,
                    "duration_ms": duration_ms,
                }

            except asyncio.CancelledError:
                duration_ms = (time.monotonic() - start_time) * 1000
                logger.warning(f"步骤 {step.id} 被取消")
                return {
                    "success": False,
                    "result": "步骤被取消",
                    "duration_ms": duration_ms,
                }
            except Exception as e:
                duration_ms = (time.monotonic() - start_time) * 1000
                logger.error(f"步骤 {step.id} 执行失败: {e}", exc_info=True)
                return {
                    "success": False,
                    "result": f"步骤执行异常: {e}",
                    "duration_ms": duration_ms,
                }

    def _run_tool_step(
        self, step: PlanStep, context: AgentContext
    ) -> str:
        """在线程中执行工具步骤。"""
        from omniagent.engine.tool_executor import ToolExecutor

        tool_name = step.tool or ""
        executor = ToolExecutor()
        result = executor.execute(tool_name, step.params, context, tracker=self.tracker)

        if result.success:
            return result.summary or result.format_notification()
        return result.error or result.summary or f"工具 {tool_name} 执行失败"

    def _run_llm_step(
        self, react_input: str, context: AgentContext
    ) -> str:
        """在线程中执行 LLM 步骤（使用 ReAct 引擎的 mini loop）。"""
        from omniagent.engine.react_engine import ReActEngine

        engine = ReActEngine(
            model_priority=self.model_priority,
            max_iterations=self.react_iterations,
        )
        return engine.run(react_input, context=context)

    @staticmethod
    def _extract_discoveries(step: PlanStep, discovered_info: list[str]) -> None:
        """从步骤结果中提取关键发现，供后续波次共享。"""
        import re

        result = step.result
        if not result:
            return

        # 提取文件路径
        paths = re.findall(
            # 长扩展名在前，避免 .json → .js、.yaml → .yml 的部分匹配
            r'[\w/\\.-]+\.(?:json|yaml|yml|toml|py|ts|js|md|txt|bat|ps1|png|ico)',
            result,
        )
        for p in paths[:5]:
            entry = f"文件: {p}"
            if entry not in discovered_info:
                discovered_info.append(entry)

        # 提取操作系统信息
        for keyword, label in [("Windows", "操作系统: Windows"), ("Linux", "操作系统: Linux")]:
            if keyword in result and label not in discovered_info:
                discovered_info.append(label)

    def _summarize(
        self, user_input: str, results: list[dict[str, Any]], *, analysis: str = "",
    ) -> str:
        """汇总所有步骤的结果。"""
        if not results:
            return "未执行任何步骤。"

        # 单步骤结果直接返回
        if len(results) == 1 and len(results[0].get("result", "")) < 500:
            return results[0]["result"]

        # 多步骤 → LLM 汇总
        results_text = "\n\n".join(
            f"## 步骤 {r['step_id']}: {r['task']}\n{r['result']}"
            for r in results
        )

        from omniagent.utils.llm_client import chat_completion

        analysis_block = f"\n\n任务分析: {analysis}" if analysis else ""
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个任务汇总专家。请根据各步骤的执行结果，给出最终的完整回答。"
                    "整合所有步骤的输出，形成连贯的结论。用中文回答。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"原始任务: {user_input}{analysis_block}\n\n各步骤执行结果:\n{results_text}"
                ),
            },
        ]

        try:
            for model_id in self.model_priority:
                try:
                    result = chat_completion(
                        model_id, messages, max_tokens=4096, temperature=0.5,
                    )
                    if result and result.strip():
                        return result
                except Exception:
                    continue
        except Exception:
            pass

        # LLM 汇总失败 → 返回原始结果拼接
        return "\n\n---\n\n".join(
            f"### 步骤 {r['step_id']}: {r['task']}\n{r['result']}"
            for r in results
        )


# ── 辅助函数 ────────────────────────────────────────────────────


def plan_has_dependency_annotations(plan: dict[str, Any]) -> bool:
    """快速检查 plan dict 是否包含依赖标注（depends_on 字段）。

    在调用 PlanDAG.from_plan() 之前使用，避免不必要的 DAG 构建。
    注意：有依赖标注 ≠ 有并行机会（线性链无并行但每步都有 depends_on）。
    真正判断并行需通过 PlanDAG.has_parallelism。
    """
    steps = plan.get("steps", [])
    if not steps:
        return False

    deps_found = False
    for s in steps:
        if isinstance(s, dict):
            deps = s.get("depends_on", [])
            if deps and isinstance(deps, list) and len(deps) > 0:
                deps_found = True
                break

    return deps_found
