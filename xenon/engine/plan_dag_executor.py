"""Plan-Execute 引擎的 DAG 波次执行器（纯移动自 plan_execute_engine.py）。

v0.8.4 拆分第一步：_run_dag / _exec_wave_serial / _exec_wave_parallel /
_declared_paths / _wave_parallel_blocker / _build_prev_results / _step_outcome
七个方法与「计划生成/补写/验证循环」无耦合，仅依赖 BaseEngine 提供的
共享状态（callback / enable_parallel / _execute_tool_step / steering 原语）。
作为 Mixin 挂回 PlanExecuteEngine，行为零变化——diff 即移动证明。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xenon.engine.context import AgentContext
from xenon.engine.plan_dag import PlanDAG, PlanDAGCycleError
from xenon.engine.tool_tracker import ToolExecutionTracker
from xenon.nodes.tool_executor import ToolExecuteResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StepOutcome:
    """Structured execution state retained until dependency scheduling completes."""

    content: str
    success: bool
    error: str | None = None


class PlanDAGExecutorMixin:
    """DAG 波次执行原语。宿主须提供 BaseEngine 的共享属性/方法。"""

    # ── Phase 2: DAG 波次执行（P2-E2） ────────────────────────
    def _run_dag(
        self,
        steps: list[dict[str, Any]],
        user_input: str,
        ctx: AgentContext,
        tracker: ToolExecutionTracker,
        total: int,
    ) -> list[dict[str, Any]]:
        """拓扑波次执行：同 wave 并发（若 enable_parallel），波次间串行。

        依赖失败/跳过的步骤级联跳过（修复审核 §8.27.1：失败步骤的依赖项不再
        盲目继续）。所有 callback 调用都在主线程发出，避免并发渲染竞争。
        """
        dag = PlanDAG(steps)  # 重复 id → ValueError；waves() → PlanDAGCycleError
        waves = dag.waves()
        logger.info(
            "DAG 执行：%d 个步骤分为 %d 个波次（并行=%s）",
            len(steps),
            len(waves),
            self.enable_parallel,
        )

        results: list[dict[str, Any]] = []
        failed_ids: set[Any] = set()
        skipped_ids: set[Any] = set()

        # Mid-task steering：波次检查点消费用户补充，注入后续串行波次。
        # 并行波次不传递 steering（并发步骤注入补充会语义混乱），
        # 补充在并行波次结束后、下一个波次检查点才被消费。
        # 若补充到达时剩余步骤全是工具步骤（确定性执行无法承载），
        # 触发剩余计划重规划并重建 DAG。
        _steer_pending: list[dict[str, Any]] = []

        wave_index = 0
        while wave_index < len(waves):
            wave = waves[wave_index]
            if self._interrupted:
                self.callback.on_warning("引擎被用户中断，停止执行")
                logger.info("Plan-Execute DAG 被中断，退出波次循环")
                break
            _steer_pending.extend(self._drain_steering())

            # 若 steering 已到达且本波次全是工具步骤（无法承载补充）：
            # 剩余计划重规划，重建 DAG 从头调度（已完成步骤按 id 过滤）。
            if _steer_pending:
                wave_tools = [dag.step(sid).get("tool") for sid in wave]
                wave_all_tool = bool(wave_tools) and all(
                    t and t != "null" for t in wave_tools
                )
                if wave_all_tool:
                    done_ids = {r["step_id"] for r in results}
                    new_steps = self._replan_remaining(
                        user_input,
                        ctx,
                        _steer_pending,
                        done_ids,
                    )
                    if new_steps:
                        steps = new_steps
                        try:
                            dag = PlanDAG(new_steps)
                        except (PlanDAGCycleError, ValueError) as exc:
                            logger.warning(
                                "补充后重规划 DAG 无效 (%s)，沿用原计划", exc
                            )
                            self.callback.on_warning(
                                f"补充后重规划依赖图无效，沿用原计划：{exc}"
                            )
                        else:
                            waves = dag.waves()
                            wave_index = 0
                            self.callback.on_warning(
                                f"已收到补充要求，剩余 {len(new_steps)} 步已重新规划"
                            )
                    _steer_pending = []
                    # 重规划成功 → 已从头调度（wave_index=0）；
                    # 重规划失败/沿用原计划 → 前进索引继续原波次。
                    # 两种路径都不能让 while 停在原地（死循环防护）。
                    if wave_index != 0:
                        wave_index += 1
                    continue

            # 划分：依赖失败/跳过的步骤级联跳过，其余可执行
            dep_map = dag.dependency_map()
            to_skip: list[Any] = []
            to_run: list[Any] = []
            for sid in wave:
                deps = dep_map.get(sid, [])
                if any(d in failed_ids or d in skipped_ids for d in deps):
                    to_skip.append(sid)
                else:
                    to_run.append(sid)

            # 跳过的步骤：记录 + 回调（主线程）
            for sid in to_skip:
                step = dag.step(sid)
                step_task = step.get("task", "")
                result = "⏭️ 步骤已跳过：前置依赖失败或被跳过"
                self.callback.on_step(sid, total, step_task)
                results.append(
                    {
                        "step_id": sid,
                        "task": step_task,
                        "result": result,
                        "status": "skipped",
                        "error": "前置依赖失败或被跳过",
                    }
                )
                ctx.set(f"step_{sid}_result", result)
                ctx.set(f"step_{sid}_status", "skipped")
                skipped_ids.add(sid)
                self.callback.on_step_done(sid, False, result[:200])

            if not to_run:
                wave_index += 1  # while 循环：波次全跳过也必须前进索引
                continue

            # 可执行步骤：先发 on_step（主线程，按波内顺序），再执行
            for sid in to_run:
                self.callback.on_step(sid, total, dag.step(sid).get("task", ""))

            parallel_downgrade: str | None = None
            if self.enable_parallel and len(to_run) > 1:
                parallel_downgrade = self._wave_parallel_blocker(to_run, dag)
                if parallel_downgrade is not None:
                    # 用户显式开了并行，降级必须可见，否则只会觉得"并行没生效"。
                    self.callback.on_warning(
                        f"本波次退回串行以保护工作区：{parallel_downgrade}"
                    )
            if self.enable_parallel and len(to_run) > 1 and parallel_downgrade is None:
                wave_results = self._exec_wave_parallel(
                    to_run,
                    dag,
                    user_input,
                    results,
                    ctx,
                    total,
                )
            else:
                wave_results = self._exec_wave_serial(
                    to_run,
                    dag,
                    user_input,
                    results,
                    ctx,
                    tracker,
                    total,
                    steering=_steer_pending,
                )
            _steer_pending = []

            # 合并（主线程，单线程，无竞争）：追加结果 + 合并隔离 tracker
            for sid, step_task, outcome, sub_tracker in wave_results:
                status = "ok" if outcome.success else "failed"
                results.append(
                    {
                        "step_id": sid,
                        "task": step_task,
                        "result": outcome.content,
                        "status": status,
                        "error": outcome.error,
                    }
                )
                ctx.set(f"step_{sid}_result", outcome.content)
                ctx.set(f"step_{sid}_status", status)
                if sub_tracker is not None:
                    tracker.calls.extend(sub_tracker.calls)
                if not outcome.success:
                    failed_ids.add(sid)
                self.callback.on_step_done(sid, outcome.success, outcome.content[:200])
                logger.debug(f"步骤 {sid} 完成: {outcome.content[:100]}")

            if ctx.get("_task_cancelled"):
                self.callback.on_warning("用户取消任务，停止后续计划步骤")
                break
            wave_index += 1

        return results

    def _exec_wave_serial(
        self,
        to_run: list[Any],
        dag: PlanDAG,
        user_input: str,
        results: list[dict[str, Any]],
        ctx: AgentContext,
        tracker: ToolExecutionTracker,
        total: int,
        steering: list[dict[str, Any]] | None = None,
    ) -> list[tuple[Any, str, StepOutcome, None]]:
        """波内串行执行（共享主 ctx/tracker，无并发无竞争）。

        返回 [(sid, task, result, None)]，sub_tracker=None 表示已直接写入主 tracker。

        单步抛异常转为失败结果，不连坐整波——与 ``_exec_wave_parallel`` 同一契约。
        串行是默认路径，此前缺少这层隔离：任意一步抛异常会直接冒泡终止整个 DAG，
        已完成步骤的结果和后续可执行步骤一起丢失。

        ``steering``：波次检查点收集到的用户中途补充，合并进本波步骤执行。
        """
        out: list[tuple[Any, str, StepOutcome, None]] = []
        for sid in to_run:
            step = dag.step(sid)
            step_task = step.get("task", "")
            tool = step.get("tool")
            params = step.get("params", {})
            prev_results = self._build_prev_results(results)
            try:
                if tool and tool != "null":
                    outcome = self._execute_tool_step(
                        sid,
                        total,
                        step_task,
                        tool,
                        params,
                        user_input,
                        ctx,
                        tracker,
                        prev_results,
                    )
                else:
                    raw_result = self._execute_step_with_llm(
                        sid,
                        total,
                        step_task,
                        prev_results,
                        user_input,
                        tracker,
                        context=ctx,
                        steering=steering,
                    )
                    outcome = self._step_outcome(raw_result)
            except Exception as e:  # 单步异常不连坐整波
                logger.exception("DAG 串行步骤 %r 执行异常", sid)
                outcome = StepOutcome(f"执行异常: {e}", False, str(e))
            out.append((sid, step_task, outcome, None))
        return out

    @staticmethod
    def _declared_paths(tool: str, params: dict[str, Any]) -> list[str] | None:
        """列出某步骤声明会触及的路径；无法枚举时返回 ``None``。

        ``None`` 表示「足迹不可知」，调用方必须按最坏情况处理（退回串行）。
        返回空列表表示该工具确实不触及任何路径。
        """
        if not isinstance(params, dict):
            return None

        def _norm(raw: Any) -> str | None:
            text = str(raw or "").strip()
            if not text:
                return None
            try:
                return str(Path(text).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                return text

        # 批量工具的路径藏在列表里，逐项枚举
        if tool == "batch_write":
            specs = params.get("files")
            if not isinstance(specs, list):
                return None
            paths = []
            for spec in specs:
                if not isinstance(spec, dict):
                    return None
                path = _norm(spec.get("path") or spec.get("file_path"))
                if path is None:
                    return None
                paths.append(path)
            return paths
        if tool == "batch_edit":
            specs = params.get("edits")
            if not isinstance(specs, list):
                return None
            paths = []
            for spec in specs:
                if not isinstance(spec, dict):
                    return None
                path = _norm(spec.get("file_path") or spec.get("path"))
                if path is None:
                    return None
                paths.append(path)
            return paths

        single = _norm(params.get("file_path") or params.get("path"))
        return [single] if single else []

    def _wave_parallel_blocker(
        self,
        to_run: list[Any],
        dag: PlanDAG,
    ) -> str | None:
        """判断波次能否真正并发；可以则返回 ``None``，否则返回人类可读的原因。

        按**声明资源冲突**判定，而非工具黑名单。``_exec_wave_parallel`` 隔离了
        ctx/tracker，解决的是**进程内**数据竞争；它不能防止两个步骤并发读写同一个
        文件造成的**工作区**竞态。planner 是 LLM 生成的，完全可能把冲突步骤放进同
        一波次——prompt 约束不是安全边界，必须在代码层判定。

        判定规则（结构性，不枚举领域关键词）：

        1. **足迹不可知 → 整波串行**：LLM 步骤（``tool`` 为空）内部跑迷你 ReAct，
           可调用任意工具（含写入与 shell）；``SENSITIVE``（任意 shell）与仓库级
           工具（git/clone_repo 等）影响面无法静态枚举；写工具但路径无法枚举同样
           按最坏情况处理。
        2. **同一路径被写、且被一个以上步骤触及 → 整波串行**（写写、读写都算）。
        3. 其余情况（写不同路径、或全是只读）允许并发——并发读不损坏工作区。

        因此「写 a.py + 写 b.py」仍可并行，而「两步都写 a.py」会被挡下。
        """
        from xenon.nodes.tool_executor import classify_tool

        # 影响面无法静态枚举的工具：仓库级/远端/派生 Agent
        opaque_tools = {
            "git",
            "clone_repo",
            "register_tool",
            "mcp_call",
            "spawn_agent",
        }
        readers: dict[str, list[Any]] = {}
        writers: dict[str, list[Any]] = {}

        for sid in to_run:
            step = dag.step(sid)
            tool = step.get("tool")
            params = step.get("params", {}) or {}
            if not tool or tool == "null":
                return f"步骤 {sid} 由 LLM 执行，可能调用任意工具，足迹不可预知"
            if tool in opaque_tools or classify_tool(tool, params) == "SENSITIVE":
                return f"步骤 {sid} 使用 {tool}，影响范围无法静态判定"

            paths = self._declared_paths(tool, params)
            is_reader = tool in self._PARALLEL_SAFE_TOOLS
            if paths is None:
                if is_reader:
                    # 只读工具即使路径不可知也不会破坏工作区
                    continue
                return f"步骤 {sid} 的写入路径无法枚举（{tool}）"
            target = readers if is_reader else writers
            for path in paths:
                target.setdefault(path, []).append(sid)

        for path, writer_ids in writers.items():
            touching = list(writer_ids) + readers.get(path, [])
            if len(touching) > 1:
                return f"步骤 {sorted(set(touching))} 同时触及 {path}（含写入）"
        return None

    def _exec_wave_parallel(
        self,
        to_run: list[Any],
        dag: PlanDAG,
        user_input: str,
        results: list[dict[str, Any]],
        ctx: AgentContext,
        total: int,
    ) -> list[tuple[Any, str, StepOutcome, ToolExecutionTracker]]:
        """波内并发执行（ThreadPoolExecutor 包同步调用）。

        每个步骤持有**独立的隔离 ctx + tracker**（镜像 combined_engines._isolated_ctx），
        规避 ToolExecutionTracker / AgentContext.messages 无锁的数据竞争（审核
        §8.1.6）。prev_results 在主线程预先快照，worker 不读共享 list。单步异常
        被捕获转为失败结果，不连坐整波。返回结果按 to_run 原顺序排列。
        """
        # 主线程预先算好每步 prev_results 快照
        prev_map = {sid: self._build_prev_results(results) for sid in to_run}

        def work(sid: Any) -> tuple[Any, str, StepOutcome, ToolExecutionTracker]:
            step = dag.step(sid)
            step_task = step.get("task", "")
            tool = step.get("tool")
            params = step.get("params", {})
            # 隔离 ctx/tracker：仅复制对话消息作历史兜底，store/tracker 独立
            iso_ctx = AgentContext()
            iso_ctx.set_conversation_messages(list(ctx.get_conversation_messages()))
            # The worker owns an isolated store, but its durable lifecycle
            # events must be published to the parent session.  Otherwise an
            # explicitly enabled parallel Plan-Execute run disappears from
            # crash recovery even though ReAct's shared-context path works.
            if hasattr(ctx, "record_tool_checkpoint"):
                iso_ctx.set_tool_checkpoint_callback(ctx.record_tool_checkpoint)
            iso_tracker = ToolExecutionTracker()
            try:
                if tool and tool != "null":
                    outcome = self._execute_tool_step(
                        sid,
                        total,
                        step_task,
                        tool,
                        params,
                        user_input,
                        iso_ctx,
                        iso_tracker,
                        prev_map[sid],
                    )
                else:
                    raw_result = self._execute_step_with_llm(
                        sid,
                        total,
                        step_task,
                        prev_map[sid],
                        user_input,
                        iso_tracker,
                        context=iso_ctx,
                    )
                    outcome = self._step_outcome(raw_result)
            except Exception as e:  # 单步异常不连坐整波
                logger.exception("DAG 并发步骤 %r 执行异常", sid)
                outcome = StepOutcome(f"执行异常: {e}", False, str(e))
            return (sid, step_task, outcome, iso_tracker)

        workers = min(len(to_run), self.max_parallel_workers)
        collected: dict[Any, tuple[Any, str, StepOutcome, ToolExecutionTracker]] = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(work, sid) for sid in to_run]
            for fut in futures:
                sid, task, outcome, iso_tracker = fut.result()
                collected[sid] = (sid, task, outcome, iso_tracker)
        return [collected[sid] for sid in to_run]

    @staticmethod
    def _build_prev_results(results: list[dict[str, Any]]) -> str:
        """Build context from successful steps only, never from failure text."""
        successful = [
            result for result in results if result.get("status", "ok") == "ok"
        ]
        if not successful:
            return "(无)"
        return "\n".join(
            f"步骤 {r['step_id']}: {r['result'][:200]}" for r in successful[-3:]
        )

    @staticmethod
    def _step_outcome(value: Any) -> StepOutcome:
        """Normalize native tool results while preserving legacy test/extensions."""
        if isinstance(value, StepOutcome):
            return value
        if isinstance(value, ToolExecuteResult):
            return StepOutcome(
                value.format_observation(),
                value.success,
                value.error,
            )
        text = str(value or "")
        # Compatibility for custom/monkeypatched executors that still return
        # plain strings. Native ToolExecutor results never rely on this guess.
        success = not text.startswith(("执行失败", "执行异常", "工具执行失败", "⏭️"))
        return StepOutcome(text, success, None if success else text)
