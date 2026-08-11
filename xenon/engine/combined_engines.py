"""Combined reasoning engines with verified state hand-off between phases."""

from __future__ import annotations

import logging
import queue
import re
import time
from typing import TYPE_CHECKING, Any

from xenon.engine.callbacks import EngineCallback
from xenon.engine.context import AgentContext
from xenon.engine.execution_evidence import ExecutionEvidence, workspace_root_for
from xenon.engine.execution_policy import EngineDeadlineExceeded
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.react_engine import ReActEngine
from xenon.engine.reflection_engine import ReflectionEngine
from xenon.engine.tool_tracker import ToolExecutionTracker

if TYPE_CHECKING:
    from xenon.repl.context_manager import ContextManager

logger = logging.getLogger(__name__)


class SteeringMixin:
    """组合引擎共享的 mid-task steering 原语。

    组合引擎不是 BaseEngine 子类（无 steer 通道自动继承），但 REPL 只
    持有顶层引擎引用。此 mixin 提供与 BaseEngine 相同语义的队列/消费/
    重置原语，组合引擎在 run() 的阶段边界消费补充要求，并拼进传给
    子引擎的 prompt——子引擎 run() 起点会 _reset_steering()，因此
    steering 必须在组合层持有，不能预注入子引擎。
    """

    def __init__(self) -> None:
        self._steering_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.steering_consumed: list[dict[str, Any]] = []

    def steer(self, text: str) -> bool:
        """任务运行中注入一条用户补充/修改要求（线程安全）。"""
        if not text or not text.strip():
            return False
        self._steering_queue.put({
            "text": text.strip(),
            "ts": time.time(),
        })
        return True

    def _drain_steering(self) -> list[dict[str, Any]]:
        """取出并记录当前所有待消费的 steering 消息（FIFO）。"""
        drained: list[dict[str, Any]] = []
        while True:
            try:
                msg = self._steering_queue.get_nowait()
            except queue.Empty:
                break
            drained.append(msg)
            self.steering_consumed.append(msg)
        return drained

    def _reset_steering(self) -> None:
        """每轮 run() 开头清空队列与消费记录。"""
        while True:
            try:
                self._steering_queue.get_nowait()
            except queue.Empty:
                break
        self.steering_consumed = []

    @staticmethod
    def steering_prompt(msgs: list[dict[str, Any]]) -> str:
        """渲染补充要求注入指令（语义与 BaseEngine.steering_prompt 一致）。"""
        from xenon.engine.base import BaseEngine
        return BaseEngine.steering_prompt(msgs)

_TEST_STEP = re.compile(r"测试|验证|回归|test|verify|check|lint", re.IGNORECASE)
_SUMMARY_STEP = re.compile(r"总结|汇总|报告|说明|summary|report", re.IGNORECASE)
_INSPECT_STEP = re.compile(
    r"分析|检查|读取|定位|调查|了解|inspect|read|analy[sz]e|investigate|locate",
    re.IGNORECASE,
)
_IMPLEMENT_STEP = re.compile(
    r"实现|修复|修改|更新|创建|编写|重构|删除|重命名|"
    r"implement|fix|modify|update|create|write|refactor|delete|rename|patch",
    re.IGNORECASE,
)
_PATH_IN_TEXT = re.compile(
    r"(?:[\w.-]+/)*[\w.-]+\.(?:py|js|ts|tsx|jsx|json|ya?ml|toml|md|txt|rs|go|java|cpp|c|h)"
)


def _isolated_ctx(ctx: AgentContext) -> AgentContext:
    """Copy durable conversation/checkpoint links, never phase-local store state."""

    fresh = AgentContext()
    fresh.set_conversation_messages(list(ctx.get_conversation_messages()))
    if hasattr(ctx, "record_tool_checkpoint"):
        fresh.set_tool_checkpoint_callback(ctx.record_tool_checkpoint)
    ledger = ctx.get("_evidence_ledger")
    session_id = ctx.get("_evidence_session_id")
    if ledger is not None:
        fresh.set("_evidence_ledger", ledger)
    if session_id:
        fresh.set("_evidence_session_id", session_id)
    if ctx.get("_strategy_tip_emitted", False):
        fresh.set("_strategy_tip_emitted", True)
    fresh.set("_strategy_phase_context", True)
    return fresh


def _merge_tracker(target: ToolExecutionTracker, source: ToolExecutionTracker | None) -> None:
    if source is not None:
        target.calls.extend(source.calls)


def _step_kind(task: str) -> str:
    if _TEST_STEP.search(task):
        return "test"
    if _SUMMARY_STEP.search(task):
        return "summary"
    if _IMPLEMENT_STEP.search(task):
        return "implement"
    if _INSPECT_STEP.search(task):
        return "inspect"
    return "other"


def _planned_paths(step: dict[str, Any]) -> set[str]:
    paths = set(_PATH_IN_TEXT.findall(str(step.get("task", ""))))
    params = step.get("params")
    if isinstance(params, dict):
        for key in ("file_path", "path"):
            value = params.get(key)
            if isinstance(value, str) and value:
                paths.add(value)
    return paths


def _skip_reason(
    step: dict[str, Any],
    evidence: ExecutionEvidence,
    *,
    implementation_steps: int,
) -> str | None:
    """Skip only when verified state already covers the planned operation."""

    if evidence.exact_call_succeeded(step.get("tool"), step.get("params")):
        return "同一工具和参数已经成功执行"

    kind = _step_kind(str(step.get("task", "")))
    if kind == "test" and evidence.successful_tests:
        return "已有成功的真实测试执行记录"
    if kind == "summary" and evidence.implementation_verified:
        return "实现与测试均已验证，最终结果将由本地证据汇总"
    if kind in {"inspect", "other"} and evidence.implementation_verified:
        return "工作区变更和测试均已完成，无需重复探索"
    if kind == "implement" and evidence.implementation_verified:
        planned = _planned_paths(step)
        changed = evidence.changed_files
        if implementation_steps == 1:
            return "唯一实现阶段已由前一步完成并通过测试"
        if planned and any(
            target == changed_path
            or target.endswith("/" + changed_path)
            or changed_path.endswith("/" + target)
            for target in planned
            for changed_path in changed
        ):
            return "计划目标文件已经修改并通过测试"
    return None


def _review_feedback(review: dict[str, Any]) -> str:
    feedback = str(review.get("feedback") or "审查未通过")
    issues = review.get("issues")
    if isinstance(issues, list) and issues:
        feedback += "\n" + "\n".join(f"- {issue}" for issue in issues[:12])
    return feedback


class PlanReactEngine(SteeringMixin):
    """Plan globally, then execute uncovered steps through isolated ReAct runs."""

    def __init__(
        self,
        model_priority: list[str],
        *,
        max_steps: int = 15,
        react_iterations: int = 8,
        callback: EngineCallback | None = None,
        model_configs: dict[str, Any] | None = None,
        model_pool: Any = None,
        auto_router: Any = None,
        permission_gate: Any = None,
    ) -> None:
        SteeringMixin.__init__(self)
        self.model_priority = model_priority
        self.max_steps = max_steps
        self.react_iterations = react_iterations
        self.callback = callback or EngineCallback()
        self.model_pool = model_pool
        self.auto_router = auto_router
        common = dict(
            callback=self.callback,
            model_configs=model_configs,
            model_pool=model_pool,
            auto_router=auto_router,
            permission_gate=permission_gate,
        )
        self.planner = PlanExecuteEngine(model_priority, max_steps=max_steps, **common)
        self.reactor = ReActEngine(
            model_priority, max_iterations=react_iterations, **common
        )
        self._last_tracker: ToolExecutionTracker | None = None
        self.last_model_used: str | None = None

    def _finalize_plan_react(self, ctx: AgentContext, output: str) -> str:
        if ctx.get("_evidence_ledger") is None:
            self.planner._begin_run()
            self.planner._bind_evidence_ledger(ctx)
        self.planner.finalize_evidence(context=ctx, output=output, tracker=self._last_tracker)
        return output

    def run(
        self,
        user_input: str,
        context: AgentContext | None = None,
        ctx_mgr: ContextManager | None = None,
    ) -> str:
        ctx = context or AgentContext()
        self._reset_steering()  # mid-task steering：每轮 run 重置
        aggregate = ToolExecutionTracker()
        self._last_tracker = aggregate
        self.planner._ctx_mgr = ctx_mgr

        try:
            plan = self.planner._plan(user_input, ctx)
        except EngineDeadlineExceeded:
            raise
        except Exception as exc:
            logger.exception("Plan-ReAct planning failed")
            return f"规划阶段失败: {exc}"

        steps = list(plan.get("steps") or [])[: self.max_steps]
        analysis = str(plan.get("analysis") or "")
        if not steps:
            self.callback.on_warning("未能生成有效的执行计划")
            return analysis or "未能生成有效的执行计划。"

        implementation_steps = sum(
            _step_kind(str(step.get("task", ""))) == "implement" for step in steps
        )
        results: list[dict[str, Any]] = []
        workspace_root = workspace_root_for(self.reactor)

        for index, step in enumerate(steps, 1):
            step_id = step.get("id", index)
            task = str(step.get("task") or "")
            evidence = ExecutionEvidence.capture(aggregate, workspace_root)
            reason = _skip_reason(
                step, evidence, implementation_steps=implementation_steps
            )
            if reason:
                result = f"步骤已跳过：{reason}"
                results.append({
                    "step_id": step_id,
                    "task": task,
                    "result": result,
                    "status": "skipped",
                    "evidence": evidence.render(max_diff_chars=2_000),
                })
                ctx.set(f"step_{step_id}_result", result)
                ctx.set(f"step_{step_id}_status", "skipped")
                self.callback.on_step_done(step_id, True, result)
                continue

            self.callback.on_step(step_id, len(steps), task)
            # Mid-task steering：步骤边界消费用户补充，并入当前步骤的
            # ReAct prompt（子引擎 run 起点会清掉预注入，必须在组合层拼）。
            steer_text = ""
            steering_msgs = self._drain_steering()
            if steering_msgs:
                steer_text = "\n\n" + self.steering_prompt(steering_msgs)
                self.callback.on_warning(
                    f"已收到 {len(steering_msgs)} 条补充要求，正在调整当前步骤…"
                )
            phase_ctx = _isolated_ctx(ctx)
            phase_ctx.set("combined_completed_steps", [
                {
                    "step_id": item["step_id"],
                    "task": item["task"],
                    "status": item["status"],
                }
                for item in results
            ])
            react_input = (
                f"全局任务:\n{user_input}\n\n"
                f"完整计划:\n{analysis}\n\n"
                f"当前步骤 ({step_id}/{len(steps)}):\n{task}\n\n"
                "执行规则：先核对下方真实证据；已完成的操作不要重复。"
                "如果本步骤或整个任务已经由真实状态覆盖，直接说明并结束。"
                "需要操作时必须使用工具，完成后运行适当验证。\n\n"
                f"已验证执行证据:\n{evidence.render(max_diff_chars=6_000)}"
                f"{steer_text}"
            )

            status = "ok"
            self.reactor._last_tracker = None
            try:
                result = self.reactor.run(
                    react_input, context=phase_ctx, ctx_mgr=ctx_mgr
                )
                result = result.strip() if result and result.strip() else "(步骤完成，无文本输出)"
            except EngineDeadlineExceeded:
                raise
            except Exception as exc:
                logger.exception("Plan-ReAct step %r failed", step_id)
                status = "failed"
                result = f"步骤执行失败: {exc}"

            _merge_tracker(aggregate, self.reactor._last_tracker)
            if self.reactor.last_model_used:
                self.last_model_used = self.reactor.last_model_used
            after = ExecutionEvidence.capture(aggregate, workspace_root)
            results.append({
                "step_id": step_id,
                "task": task,
                "result": result,
                "status": status,
                "evidence": after.render(max_diff_chars=2_000),
            })
            ctx.set(f"step_{step_id}_result", result)
            ctx.set(f"step_{step_id}_status", status)
            self.callback.on_step_done(step_id, status == "ok", result[:200])

        return self._finalize_plan_react(ctx, self._summarize(user_input, results, analysis))

    def _summarize(
        self,
        user_input: str,
        results: list[dict[str, Any]],
        analysis: str = "",
    ) -> str:
        ok = [result for result in results if result.get("status") == "ok"]
        failed = [result for result in results if result.get("status") == "failed"]
        evidence = ExecutionEvidence.capture(
            self._last_tracker, workspace_root_for(self.reactor)
        )

        # Coding/tool runs already have a final ReAct answer plus verified state.
        # A fresh LLM summary used to be a costly, lossy phase that could invent a
        # patch different from the real worktree.
        if ok and (evidence.has_workspace_change or evidence.successful_tests):
            answer = ok[-1]["result"]
            if failed:
                answer += "\n\n未恢复的步骤失败:\n" + "\n".join(
                    f"- {item['task']}: {item['result']}" for item in failed
                )
            return answer

        successful_text = "\n\n".join(
            f"## 步骤 {item['step_id']}: {item['task']}\n{item['result']}"
            for item in ok
        ) or "(无成功完成的步骤)"
        failed_text = ""
        if failed:
            failed_text = "\n\n## 失败的步骤\n" + "\n".join(
                f"- 步骤 {item['step_id']} ({item['task']}): {item['result']}"
                for item in failed
            )
        if all(len(item["result"]) < 100 for item in ok) and len(ok) <= 2:
            return f"## 执行计划\n{analysis}\n\n## 执行结果\n{successful_text}{failed_text}"

        messages = [
            {
                "role": "system",
                "content": "根据各步骤结果给出简洁、连贯的最终回答；不要编造未执行的操作。",
            },
            {
                "role": "user",
                "content": (
                    f"原始任务: {user_input}\n\n任务分析: {analysis}\n\n"
                    f"各步骤结果:\n{successful_text}{failed_text}"
                ),
            },
        ]
        try:
            output = self.planner._call_llm_for_phase(
                "summarize", messages, max_tokens=1600
            )
            if output and output.strip():
                self.last_model_used = self.planner.last_model_used
                return output
        except EngineDeadlineExceeded:
            raise
        except Exception:
            logger.warning("Plan-ReAct summary failed", exc_info=True)
        return f"## 执行计划\n{analysis}\n\n## 执行结果\n{successful_text}{failed_text}"


class _ReflectionCombination(SteeringMixin):
    """Shared strict review and one-shot tool-capable repair workflow."""

    reflector: ReflectionEngine
    repairer: ReActEngine
    review_rounds: int
    callback: EngineCallback
    _last_tracker: ToolExecutionTracker | None

    def __init__(self) -> None:
        SteeringMixin.__init__(self)

    def _finalize_combined(self, ctx: AgentContext, output: str) -> str:
        """Close the composite lifecycle at one shared delivery boundary."""
        finalizer = getattr(self, "repairer", None) or getattr(self, "reactor", None)
        if finalizer is None:
            raise RuntimeError("combined engine has no finalizer")
        if ctx.get("_evidence_ledger") is None:
            finalizer._begin_run()
            finalizer._bind_evidence_ledger(ctx)
        finalizer.finalize_evidence(context=ctx, output=output, tracker=self._last_tracker)
        return output

    def _review_and_repair(
        self,
        user_input: str,
        initial_output: str,
        initial_tracker: ToolExecutionTracker | None,
        initial_model_used: str | None,
        ctx: AgentContext,
        ctx_mgr: ContextManager | None,
    ) -> str:
        aggregate = ToolExecutionTracker()
        _merge_tracker(aggregate, initial_tracker)
        self._last_tracker = aggregate
        self.last_model_used = initial_model_used
        root = workspace_root_for(self.repairer)
        initial_evidence = ExecutionEvidence.capture(aggregate, root)
        state_change_required = bool(_IMPLEMENT_STEP.search(user_input))
        review_ctx = _isolated_ctx(ctx)

        # Mid-task steering：必须在 review 之前消费——若 review pass 直接
        # 收尾（下方 return），steering 会永远留在队列里被静默丢弃。
        # 用户补充/修改要求说明任务尚未按新意图完成，必须进入修复阶段。
        steering_msgs = self._drain_steering()

        try:
            review = self.reflector.review_existing(
                user_input,
                initial_output,
                evidence=initial_evidence.render(),
                context=review_ctx,
                ctx_mgr=ctx_mgr,
            )
        except EngineDeadlineExceeded:
            raise
        except Exception as exc:
            logger.warning("Reflection review failed; preserving executed result: %s", exc)
            self.callback.on_error(f"Reflection 审查失败: {exc}")
            return self._finalize_combined(ctx, initial_output)

        # A reviewer score cannot certify a write task when no mutating tool
        # actually succeeded.  This programmatic gate fixes the former
        # "read-only plan + self-score 10" false success mode.
        if review.get("pass") and not steering_msgs and not (
            state_change_required and initial_evidence.mutation_count == 0
        ):
            return self._finalize_combined(ctx, initial_output)
        if review.get("pass"):
            # review 通过但存在用户补充（任务未按新意图完成），或写任务
            # 无落地证据：都不能直接收尾，强制进入修复阶段。
            if steering_msgs:
                review = {
                    **review,
                    "pass": False,
                    "feedback": "用户补充/修改了任务要求，请按补充要求继续调整",
                    "issues": ["用户补充要求尚未反映在输出中"],
                }
            else:
                review = {
                    **review,
                    "pass": False,
                    "feedback": "任务要求修改状态，但没有成功的状态变更工具记录",
                    "issues": ["工作区修改尚未通过工具落地"],
                }

        repair_prompt = (
            f"原始任务:\n{user_input}\n\n"
            f"当前执行结果:\n{initial_output}\n\n"
            f"审查反馈:\n{_review_feedback(review)}\n\n"
            "修正规则：以真实工作区和工具结果为准。先检查现状，只修复审查指出且"
            "确实存在的问题；需要改变文件时必须用工具落地，并运行聚焦验证。"
            "不要只输出一个未应用的补丁，也不要重复已经成功的操作。\n\n"
            f"真实执行证据:\n{initial_evidence.render(max_diff_chars=10_000)}"
        )
        if steering_msgs:
            repair_prompt += "\n\n" + self.steering_prompt(steering_msgs)
            self.callback.on_warning(
                f"已收到 {len(steering_msgs)} 条补充要求，正在调整修复…"
            )
        repair_ctx = _isolated_ctx(ctx)
        self.repairer._last_tracker = None
        try:
            repaired_output = self.repairer.run(
                repair_prompt, context=repair_ctx, ctx_mgr=ctx_mgr
            )
        except EngineDeadlineExceeded:
            raise
        except Exception as exc:
            logger.warning("Reflection repair failed; preserving executed result: %s", exc)
            self.callback.on_error(f"Reflection 修复失败: {exc}")
            return self._finalize_combined(ctx, initial_output)

        repair_tracker = self.repairer._last_tracker
        _merge_tracker(aggregate, repair_tracker)
        repaired_evidence = ExecutionEvidence.capture(aggregate, root)
        repair_changed_state = bool(
            repair_tracker
            and any(call.success for call in repair_tracker.calls)
            and repaired_evidence.mutation_count > initial_evidence.mutation_count
        )
        if state_change_required and not repair_changed_state:
            logger.warning(
                "Reflection repair made no verified state change; preserving initial output"
            )
            return self._finalize_combined(ctx, initial_output)

        if self.repairer.last_model_used:
            self.last_model_used = self.repairer.last_model_used

        if self.review_rounds > 1:
            try:
                post_review = self.reflector.review_existing(
                    user_input,
                    repaired_output,
                    evidence=repaired_evidence.render(),
                    context=_isolated_ctx(ctx),
                    ctx_mgr=ctx_mgr,
                )
                if not post_review.get("pass"):
                    self.callback.on_warning(
                        "修复已落地，但复审仍未通过；保留真实工作区并报告当前结果"
                    )
            except EngineDeadlineExceeded:
                raise
            except Exception as exc:
                logger.warning("Post-repair review failed: %s", exc)

        return self._finalize_combined(ctx, repaired_output or initial_output)


class PlanReflectionEngine(_ReflectionCombination):
    """Plan-Execute followed by evidence review and one tool-capable repair."""

    def __init__(
        self,
        model_priority: list[str],
        *,
        max_steps: int = 15,
        review_rounds: int = 2,
        pass_threshold: int = 7,
        repair_iterations: int | None = None,
        callback: EngineCallback | None = None,
        model_configs: dict[str, Any] | None = None,
        model_pool: Any = None,
        auto_router: Any = None,
        permission_gate: Any = None,
    ) -> None:
        SteeringMixin.__init__(self)
        self.model_priority = model_priority
        self.max_steps = max_steps
        self.review_rounds = review_rounds
        self.pass_threshold = pass_threshold
        self.callback = callback or EngineCallback()
        self.model_pool = model_pool
        self.auto_router = auto_router
        common = dict(
            callback=self.callback,
            model_configs=model_configs,
            model_pool=model_pool,
            auto_router=auto_router,
            permission_gate=permission_gate,
        )
        self.planner = PlanExecuteEngine(model_priority, max_steps=max_steps, **common)
        self.reflector = ReflectionEngine(
            model_priority,
            max_rounds=review_rounds,
            pass_threshold=pass_threshold,
            **common,
        )
        repair_budget = (
            min(max_steps, 10) if repair_iterations is None else repair_iterations
        )
        self.repairer = ReActEngine(
            model_priority, max_iterations=max(1, repair_budget), **common
        )
        self._last_tracker: ToolExecutionTracker | None = None
        self.last_model_used: str | None = None

    def run(
        self,
        user_input: str,
        context: AgentContext | None = None,
        ctx_mgr: ContextManager | None = None,
    ) -> str:
        ctx = context or AgentContext()
        self._reset_steering()  # mid-task steering：每轮 run 重置
        initial = self.planner.run(user_input, context=ctx, ctx_mgr=ctx_mgr)
        return self._review_and_repair(
            user_input,
            initial,
            self.planner._last_tracker,
            self.planner.last_model_used,
            ctx,
            ctx_mgr,
        )


class ReactReflectionEngine(_ReflectionCombination):
    """ReAct followed by evidence review and one bounded corrective ReAct run."""

    def __init__(
        self,
        model_priority: list[str],
        *,
        react_iterations: int = 8,
        review_rounds: int = 2,
        pass_threshold: int = 7,
        callback: EngineCallback | None = None,
        model_configs: dict[str, Any] | None = None,
        model_pool: Any = None,
        auto_router: Any = None,
        permission_gate: Any = None,
    ) -> None:
        SteeringMixin.__init__(self)
        self.model_priority = model_priority
        self.react_iterations = react_iterations
        self.review_rounds = review_rounds
        self.pass_threshold = pass_threshold
        self.callback = callback or EngineCallback()
        self.model_pool = model_pool
        self.auto_router = auto_router
        common = dict(
            callback=self.callback,
            model_configs=model_configs,
            model_pool=model_pool,
            auto_router=auto_router,
            permission_gate=permission_gate,
        )
        self.reactor = ReActEngine(
            model_priority, max_iterations=react_iterations, **common
        )
        # A separate repairer prevents the initial ReAct tracker and loop state
        # from being reset before the combination has aggregated its evidence.
        self.repairer = ReActEngine(
            model_priority,
            max_iterations=max(1, min(react_iterations, 10)),
            **common,
        )
        self.reflector = ReflectionEngine(
            model_priority,
            max_rounds=review_rounds,
            pass_threshold=pass_threshold,
            **common,
        )
        self._last_tracker: ToolExecutionTracker | None = None
        self.last_model_used: str | None = None

    def run(
        self,
        user_input: str,
        context: AgentContext | None = None,
        ctx_mgr: ContextManager | None = None,
    ) -> str:
        ctx = context or AgentContext()
        self._reset_steering()  # mid-task steering：每轮 run 重置
        initial = self.reactor.run(user_input, context=ctx, ctx_mgr=ctx_mgr)
        return self._review_and_repair(
            user_input,
            initial,
            self.reactor._last_tracker,
            self.reactor.last_model_used,
            ctx,
            ctx_mgr,
        )
