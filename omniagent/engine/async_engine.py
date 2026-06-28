"""异步事件驱动引擎 — 基于 ReActEngine 的异步子类。

AsyncReActEngine 继承自 ReActEngine，仅覆盖 I/O 方法为异步版本。
所有核心逻辑（探索预算、空洞检测、怜悯编译、上下文压缩等）从父类继承，
彻底消除之前的完整代码拷贝。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from omniagent.engine.base_engine import AsyncBaseEngine
from omniagent.engine.callbacks import EngineCallback
from omniagent.engine.compactor import Compactor
from omniagent.engine.context import AgentContext
from omniagent.engine.react_engine import (
    BUILTIN_TOOLS,
    REACT_SYSTEM_PROMPT,
    ReActEngine,
    _build_observation_summary,
    _check_hollow_answer,
    _compile_exhaustion_report,
    _extract_last_observation,
)
from omniagent.engine.tool_tracker import ToolExecutionTracker
from omniagent.utils.llm_client import chat_completion_async
from omniagent.utils.response_adapter import parse_react

logger = logging.getLogger(__name__)


class AsyncReActEngine(ReActEngine):
    """异步 ReAct 引擎 — 继承自 ReActEngine，仅覆盖 I/O 为异步。

    与父类共享所有核心逻辑:
    - 系统提示词构建 (_build_system_prompt)
    - 探索预算管理
    - 空洞检测 (_check_hollow_answer)
    - 怜悯编译 (_mercy_compile)
    - 上下文压缩 (Compactor)
    - 工具断路器
    - JSON 解析 (_parse_response)
    - 输入分析 (_input_requires_tools)

    仅覆盖方法:
    - __init__: 新增 event_bus / trace_writer / tool_registry
    - run: 异步事件循环
    - _call_llm: 异步 HTTP 调用

    代码量: ~400 行 (之前 727 行, 减少 45%)
    """

    def __init__(
        self,
        model_priority: list[str],
        *,
        max_iterations: int = 10,
        system_prompt: str | None = None,
        tools: dict[str, dict] | None = None,
        callback: EngineCallback | None = None,
        # ── 异步专属 ──
        event_bus: Any = None,
        trace_writer: Any = None,
        tool_registry: Any = None,
        permission_manager: Any = None,
        session_id: str = "",
        **kwargs,
    ) -> None:
        # 调用父类 __init__ 设置所有核心属性
        super().__init__(
            model_priority=model_priority,
            max_iterations=max_iterations,
            system_prompt=system_prompt or REACT_SYSTEM_PROMPT,
            tools=tools,
            callback=callback,
            **kwargs,
        )

        # 异步专属组件
        self._event_bus = event_bus
        self._trace = trace_writer
        self._tool_registry = tool_registry
        self._permissions = permission_manager
        self.session_id = session_id

    # ── 主循环 ────────────────────────────────────────────────

    async def run(self, user_input: str, context: AgentContext | None = None) -> str:
        """异步执行 ReAct 循环。

        Args:
            user_input: 用户输入
            context: 可选的共享上下文

        Returns:
            最终答案文本
        """
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        ctx = context or AgentContext()
        tracker = ToolExecutionTracker()
        messages: list[dict[str, str]] = [{"role": "system", "content": self.system_prompt}]

        # 注入对话历史
        self._inject_history(messages, ctx)
        messages.append({"role": "user", "content": user_input})

        # ── 初始化上下文压缩器 ──
        from pathlib import Path as _Path
        session_dir = _Path.cwd() / ".omniagent" / "sessions" / (self.session_id or run_id)
        compactor = Compactor(session_dir)
        compact_count = 0

        # ── 发布 RunStartedEvent ──
        await self._publish_event("run.started", run_id=run_id, goal=user_input, mode="react",
                                   model_ids=self.model_priority, session_id=self.session_id,
                                   cwd=str(__import__("pathlib").Path.cwd()))

        # ── Trace: open run ──
        if self._trace:
            self._trace.open_run(run_id)
            self._trace.emit_ipc("CLI→CORE", {"run_id": run_id, "goal": user_input},
                                 run_id=run_id, kind="agent_run")

        requires_tools = self._input_requires_tools(user_input)
        no_tool_streak = 0

        try:
            for i in range(self.max_iterations):
                # ── 上下文压缩检查（按可配置间隔 + 初始检查）──
                if i == 0 or (i > 0 and i % self.compact_interval == 0):
                    estimated = Compactor._estimate_tokens(messages)
                    if compactor.needs_compact(estimated):
                        logger.info(f"AsyncReAct: 第 {i} 轮触发压缩 (≈{estimated} tokens)")
                        result = compactor.compact(messages[1:], self.model_priority)
                        if result:
                            compacted = compactor.apply_compact(messages[1:], result)
                            messages = [messages[0]] + compacted
                            compact_count += 1
                            logger.info(
                                f"AsyncReAct: 压缩完成 (第 {compact_count} 次), "
                                f"{result.original_token_estimate} → {result.summary_tokens} tokens"
                            )

                logger.debug(f"AsyncReAct 迭代 {i + 1}/{self.max_iterations}")

                # ── 发布 StepStartedEvent ──
                await self._publish_event("step.started", run_id=run_id, step=i + 1)

                # ── 接近上限时注入合成提示（与 ReactEngine 对齐）──
                remaining = self.max_iterations - i
                if remaining <= self.hurry_warning_threshold and i > 0 and not tracker.has_executions():
                    hurry_msg = (
                        f"⚠️ 注意：仅剩 {remaining} 轮迭代机会。"
                        f"请立即使用工具开始执行任务，不要再只做探索。"
                        f"用 list_files 了解结构，用 read_file 读取关键文件。"
                    )
                    messages.append({"role": "user", "content": hurry_msg})
                    logger.info(f"AsyncReAct: 注入加速提示 (剩余 {remaining} 轮，无工具执行)")
                elif remaining <= self.force_synthesis_threshold and tracker.has_executions():
                    obs_summary = _build_observation_summary(messages, tracker)
                    hurry_msg = (
                        f"🛑 仅剩 {remaining} 轮！这是你的最后机会。\n\n"
                        f"## 你已收集的数据摘要\n{obs_summary}\n\n"
                        f"## 你现在必须做的事\n"
                        f"立即输出 final_answer，基于上述已收集的数据直接交付完整的分析报告。\n"
                        f"❌ 不要再调用 read_file/list_files —— 你没有时间了\n"
                        f"❌ 不要输出 \"我将...\" \"继续...\" —— 直接写报告内容\n"
                        f"✅ 输出格式：{{\"thought\": \"基于已收集数据总结...\", \"final_answer\": \"## 分析报告\\n\\n### 1. 项目概述\\n...\"}}\n"
                        f"final_answer 必须是可直接交付的完整报告。"
                    )
                    messages.append({"role": "user", "content": hurry_msg})
                    logger.info(f"AsyncReAct: 注入强制合成提示 (剩余 {remaining} 轮，含观察摘要)")
                elif remaining <= self.hurry_warning_threshold and tracker.has_executions() and len(tracker.calls) >= self.midpoint_check_calls:
                    midpoint_msg = (
                        f"⚠️ 探索预算提醒：你已执行 {len(tracker.calls)} 次工具调用，剩余 {remaining} 轮。\n"
                        f"根据探索预算规则，你应该已经读了核心源文件，现在准备合成 final_answer。\n"
                        f"如果还在读 build 脚本/配置文件/README，立即停止——直接基于已有数据输出 final_answer。"
                    )
                    messages.append({"role": "user", "content": midpoint_msg})
                    logger.info(f"AsyncReAct: 中点预算提醒 (已 {len(tracker.calls)} 次调用，剩余 {remaining} 轮)")

                # ── Trace: LLM call ──
                if self._trace:
                    self._trace.emit_llm("CORE→LLM", model=self.model_priority[0],
                                         run_id=run_id, kind="react_iteration",
                                         data={"iteration": i + 1, "message_count": len(messages)})

                # 异步调用 LLM（P2-Fix7: 优先原生工具调用）
                native_response = await self._call_llm_native_async(messages)

                # ── Trace: LLM response ──
                if self._trace:
                    self._trace.emit_llm("LLM→CORE", model=self.model_priority[0],
                                         run_id=run_id, kind="react_response",
                                         data={"response_len": len(native_response.get("raw_text", ""))})

                response_text = native_response.get("raw_text", "")
                messages.append({"role": "assistant", "content": response_text})

                # 解析 LLM 输出（优先使用原生 tool_calls）
                parsed = native_response

                # ── 处理 JSON 解析失败 ──
                if parsed.get("parse_error"):
                    logger.warning(f"AsyncReAct: 第 {i + 1} 轮 JSON 解析失败，要求 LLM 重试")
                    self.callback.on_warning(f"LLM 输出格式错误，要求重试（第 {i + 1} 轮）")
                    fmt_correction = (
                        "❌ 你的上一条回复无法被解析为有效的 JSON 格式。\n\n"
                        "请严格遵守以下格式重新输出：\n\n"
                        "调用工具时：\n"
                        '```json\n{"thought": "分析当前状态", "action": "工具名", "action_input": {"参数": "值"}}\n```\n\n'
                        "任务完成时：\n"
                        '```json\n{"thought": "总结结果", "final_answer": "给用户的最终回答"}\n```\n\n'
                        "⚠️ 注意：\n"
                        "- 只输出一个 JSON 对象，不要加任何前言或后记\n"
                        "- action 必须是可用工具列表中的工具名\n"
                        "- 不要在 JSON 外添加 DSML 标记或其他文本"
                    )
                    messages.append({"role": "user", "content": fmt_correction})
                    continue

                thought = parsed.get("thought", "")
                if thought:
                    self.callback.on_think(thought)
                    await self._publish_event("agent.thought", run_id=run_id, thought=thought)

                final_answer = parsed.get("final_answer", "")
                if final_answer and final_answer.strip():
                    # ── 验证: 如果需要工具但未执行 ──
                    if requires_tools and not tracker.has_executions():
                        no_tool_streak += 1
                        if no_tool_streak <= self.max_no_tool_streak:
                            force_msg = (
                                "⚠️ 你还没有使用任何工具就声称完成了任务。"
                                "请使用工具（如 write_file、command、create_directory 等）"
                                "实际执行操作，而不是仅在文字中描述。"
                                "如果你确实不需要工具，请在 final_answer 中明确说明原因。"
                            )
                            messages.append({"role": "user", "content": force_msg})
                            self.callback.on_warning("LLM 未执行工具就声称完成，要求重试")
                            await self._publish_event("run.warning", run_id=run_id,
                                                       warning="LLM 未执行工具就声称完成")
                            continue
                        answer = final_answer
                        warning = (
                            "\n\n⚠️ **警告**: 本次回答未经工具执行验证。"
                            "LLM 声称完成了任务但未实际调用任何工具，"
                            "文件操作可能未真正执行。"
                        )
                        self.callback.on_warning("LLM 连续拒绝工具调用，附带警告返回")
                        self.callback.on_finish(answer + warning)
                        await self._publish_event("agent.final_answer", run_id=run_id,
                                                   result=answer + warning)
                        await self._publish_event("run.finished", run_id=run_id,
                                                   status="warning", result=answer)
                        return answer + warning

                    # ── hollow detection: final_answer 空洞检测 ──
                    hollow_check = _check_hollow_answer(
                        final_answer, user_input, tracker,
                        min_length=self.min_final_answer_length,
                        min_sections=self.min_structured_sections,
                    )
                    if hollow_check["is_hollow"]:
                        remaining = self.max_iterations - i
                        if remaining >= 1:
                            correction = (
                                f"❌ 你的 final_answer 不符合质量标准：{hollow_check['reason']}\n\n"
                                f"请基于已收集的所有数据，直接交付一个完整的、结构化的最终报告。\n"
                                f"不要在回答中说'我将...'、'继续...'、'基于收集到的信息...'这类元语言。\n"
                                f"直接写出分析内容本身——就像你是一个分析师在提交报告。\n"
                                f"还剩 {remaining} 轮，请立即重新输出包含完整分析内容的 final_answer。"
                            )
                            messages.append({"role": "user", "content": correction})
                            self.callback.on_warning(f"final_answer 空洞: {hollow_check['reason']}")
                            logger.warning(f"AsyncReAct: final_answer 空洞，要求重新合成 (剩余 {remaining} 轮)")
                            continue
                        logger.warning("AsyncReAct: final_answer 空洞但无剩余轮次，附带警告返回")
                        warning = (
                            "\n\n⚠️ **注意**: 最终回答可能不够完整。"
                            "建议重新运行并给出更具体的分析指令。"
                        )
                        self.callback.on_finish(final_answer + warning)
                        await self._publish_event("run.finished", run_id=run_id,
                                                   status="warning", result=final_answer)
                        return final_answer + warning

                    logger.debug(f"AsyncReAct 完成，共 {i + 1} 次迭代")
                    answer = final_answer
                    if tracker.has_executions():
                        summary = tracker.execution_summary()
                        logger.debug(f"AsyncReAct 工具执行摘要: {summary}")
                    self.callback.on_finish(answer)
                    await self._publish_event("agent.final_answer", run_id=run_id, result=answer)
                    await self._publish_event("run.finished", run_id=run_id,
                                               status="success", result=answer)
                    return answer

                if "action" in parsed:
                    action = parsed["action"]
                    action_input = parsed.get("action_input", {})

                    # ── 参数验证：文件路径不能是自然语言描述 ──
                    validated = _validate_tool_params(action, action_input)
                    if not validated["valid"]:
                        error_msg = f"参数错误: {validated['reason']}"
                        messages.append({"role": "user", "content": f"❌ {error_msg}\n请用正确的文件路径重试。"})
                        if tracker:
                            tracker.record(action, action_input, False, error_msg, error=error_msg)
                        self.callback.on_warning(f"参数验证失败: {error_msg[:100]}")
                        continue

                    logger.debug(f"AsyncReAct 行动: {action}({action_input})")
                    self.callback.on_act(action, action_input)

                    tool_use_id = f"tool-{uuid.uuid4().hex[:8]}"
                    await self._publish_event("tool.call_started", run_id=run_id,
                                               tool_use_id=tool_use_id, tool_name=action,
                                               params=action_input)

                    t_start = time.monotonic()
                    observation = await self._execute_tool_async(
                        action, action_input, ctx, tracker, tool_use_id, run_id,
                    )
                    elapsed = int((time.monotonic() - t_start) * 1000)

                    self.callback.on_observe(observation)
                    await self._publish_event("tool.call_finished", run_id=run_id,
                                               tool_use_id=tool_use_id, tool_name=action,
                                               output=observation[:500], elapsed_ms=elapsed,
                                               is_error=observation.startswith("错误"))

                    # ── 结构化观察注入（与 ReactEngine 对齐）──
                    is_error = observation.startswith("错误") or observation.startswith("⚠️") or "失败" in observation[:100]
                    obs_status = "❌ 执行失败" if is_error else "✅ 执行完成"
                    obs_msg = (
                        f"📋 工具 '{action}' 执行结果 ({obs_status}):\n"
                        f"{observation}\n\n"
                        f"请根据此结果决定下一步: "
                        f"如果成功→继续下一个操作或输出 final_answer; "
                        f"如果失败→分析原因并尝试替代方案。"
                    )
                    messages.append({"role": "user", "content": obs_msg})
                    no_tool_streak = 0

                    # ── 发布 StepFinishedEvent ──
                    await self._publish_event("step.finished", run_id=run_id, step=i + 1,
                                               success=True,
                                               summary=f"{action}: {observation[:200]}")
                else:
                    # ── thought-only 输出: 既没有 action 也没有 final_answer ──
                    remaining = self.max_iterations - i
                    if remaining >= 1 and tracker.has_executions():
                        if remaining <= 1:
                            obs_summary = _build_observation_summary(messages, tracker)
                            correction = (
                                "🛑 这是你的最后一次机会！你的上一条回复只有 thought 字段。\n\n"
                                f"## 你已收集的数据\n{obs_summary}\n\n"
                                "## 你必须立即做的\n"
                                "直接输出 final_answer，格式如下：\n"
                                '```json\n'
                                '{"thought": "基于以上数据做最终总结", '
                                '"final_answer": "## 分析报告\\n\\n'
                                '### 1. 项目概述\\n...\\n\\n'
                                '### 2. 技术栈\\n...\\n\\n'
                                '### 3. 架构分析\\n...\\n\\n'
                                '### 4. 代码质量\\n...\\n\\n'
                                '### 5. 改进建议\\n1. ...\\n2. ..."}\n'
                                '```\n'
                                "❌ 不要只输出 thought！❌ 不要调用工具！直接输出上面的 JSON！"
                            )
                        else:
                            correction = (
                                "❌ 你的上一条回复只有 thought 字段，没有 action 也没有 final_answer。\n\n"
                                "你必须做出选择：\n"
                                "1. 如果需要继续执行操作 → 输出 action + action_input\n"
                                "2. 如果任务已完成 → 输出 final_answer 直接交付最终报告\n\n"
                                f"还剩 {remaining} 轮。如果你已经收集了足够的数据，请立即输出 final_answer。\n"
                                "final_answer 必须包含完整的分析内容，不要描述'我将要做什么'。"
                            )
                        messages.append({"role": "user", "content": correction})
                        self.callback.on_warning("LLM 仅输出 thought 无 action/final_answer，要求明确表态")
                        logger.warning(f"AsyncReAct: thought-only 输出，注入选择提示 (剩余 {remaining} 轮)")
                        continue
                    if remaining >= 1:
                        correction = (
                            "❌ 你的上一条回复只有 thought 字段，没有 action 也没有 final_answer。\n\n"
                            "请立即采取行动：用 action + action_input 调用工具开始执行任务，"
                            "或者如果你的任务不需要工具，直接输出 final_answer。\n"
                            "不要只输出 thought 而不采取任何行动。"
                        )
                        messages.append({"role": "user", "content": correction})
                        self.callback.on_warning("LLM 仅输出 thought 无 action/final_answer (无工具执行)，要求行动")
                        logger.warning("AsyncReAct: thought-only 输出 (无工具执行)，注入行动提示")
                        continue
                    # 无剩余轮次，尝试从最后观察中提取
                    last_obs = _extract_last_observation(messages)
                    if last_obs and len(last_obs) > 50:
                        result = f"达到最大迭代次数，以下是最后执行结果：\n\n{last_obs[:2000]}"
                    else:
                        result = thought or response.strip() or "任务已执行，但未生成明确的回复内容。"
                    self.callback.on_finish(result)
                    await self._publish_event("run.finished", run_id=run_id,
                                               status="completed", result=result)
                    return result

            # 达到最大迭代次数 — 强制编译观察摘要
            if tracker.has_executions():
                compiled = _compile_exhaustion_report(tracker, messages, self.max_iterations)
                self.callback.on_warning(f"达到最大迭代次数，已自动编译 {len(tracker.calls)} 条观察记录")
                self.callback.on_finish(compiled)
                await self._publish_event("run.finished", run_id=run_id, status="max_iterations",
                                           result=compiled)
                return compiled

            last_obs = _extract_last_observation(messages)
            if last_obs and len(last_obs) > 100:
                msg = (
                    f"⚠️ 达到最大迭代次数 ({self.max_iterations})。\n\n"
                    f"基于收集到的数据，以下自动生成摘要：\n\n"
                    f"{last_obs[:3000]}\n\n"
                    f"💡 提示：请重新运行任务以获取更完整的分析结果。"
                )
            elif last_obs and len(last_obs) > 50:
                msg = f"达到最大迭代次数 ({self.max_iterations})，以下是最后的执行结果：\n\n{last_obs[:2000]}"
            else:
                msg = f"达到最大迭代次数 ({self.max_iterations})，未能得出最终答案。请尝试简化问题或使用更具体的指令。"
            self.callback.on_warning(msg)
            self.callback.on_finish(msg)
            await self._publish_event("run.finished", run_id=run_id, status="max_iterations",
                                       result=msg)
            return msg

        except asyncio.CancelledError:
            logger.debug(f"AsyncReAct run {run_id} 被取消")
            await self._publish_event("run.finished", run_id=run_id,
                                       status="cancelled", result="Run cancelled")
            raise

        except Exception as e:
            logger.error(f"AsyncReAct 运行异常: {e}", exc_info=True)
            await self._publish_event("run.error", run_id=run_id, error=str(e))
            await self._publish_event("run.finished", run_id=run_id,
                                       status="error", result=str(e))
            raise

        finally:
            if self._trace:
                self._trace.close_run()

    # ── LLM 调用 ──────────────────────────────────────────────

    async def _call_llm_native_async(
        self, messages: list[dict[str, str]],
        max_tokens: int = 131072,
    ) -> dict[str, Any]:
        """异步调用 LLM — 优先原生工具调用，回退 JSON 解析。

        P2-Fix7: 对齐 sync 引擎的 _call_llm_native() 逻辑。
        通过 API 的 function calling 机制获取结构化 tool_calls。
        """
        from omniagent.utils.llm_client import chat_completion_with_tools_async, NativeToolResponse
        from omniagent.utils.response_adapter import parse_react
        from omniagent.engine.react_engine import _is_substantive_answer

        last_text = ""
        for model_id in self.model_priority:
            try:
                await self._publish_event("llm.model_selected", run_id="", model=model_id)
                native: NativeToolResponse = await chat_completion_with_tools_async(
                    model_id, messages,
                    tools=self.tools,
                    max_tokens=max_tokens,
                    temperature=0.3,
                )

                # 优先: 原生 tool_calls
                if native.has_tool_calls:
                    tc = native.first_tool_call()
                    logger.debug(
                        "AsyncReAct 原生工具调用: %s(%s)",
                        tc["name"], str(tc["arguments"])[:200],
                    )
                    return {
                        "thought": f"调用工具 {tc['name']}",
                        "action": tc["name"],
                        "action_input": tc["arguments"],
                        "raw_text": native.text or f"tool_call: {tc['name']}",
                    }

                # 次优: 文本中包含 final_answer 或 JSON
                if native.text and native.text.strip():
                    last_text = native.text
                    parsed = parse_react(native.text)
                    if not parsed.get("parse_error"):
                        parsed["raw_text"] = native.text
                        return parsed
                    # 文本有意义但非 JSON → 检查是否实质性回答
                    if _is_substantive_answer(native.text):
                        return {
                            "thought": "任务完成",
                            "final_answer": native.text,
                            "raw_text": native.text,
                        }
                    logger.warning(
                        "AsyncReAct: 文本不是实质性回答 (len=%d): %.100s",
                        len(native.text), native.text.strip(),
                    )

                break

            except Exception as e:
                logger.warning(f"模型 {model_id} 原生工具调用失败: {e}，尝试下一个...")
                continue

        # 最终回退: 传统文本调用
        if not last_text:
            try:
                last_text = await chat_completion_async(
                    self.model_priority[0], messages,
                    max_tokens=max_tokens, temperature=0.3,
                )
            except Exception:
                pass

        parsed = parse_react(last_text) if last_text else {"parse_error": True, "raw_text": ""}
        parsed["raw_text"] = last_text
        return parsed

    async def _call_llm_async(self, messages: list[dict[str, str]],
                               max_tokens: int = 131072) -> str:
        """异步调用 LLM，支持多模型 fallback。（保留用于兼容）"""
        last_error = None
        for model_id in self.model_priority:
            try:
                await self._publish_event("llm.model_selected", run_id="", model=model_id)
                return await chat_completion_async(
                    model_id, messages, max_tokens=max_tokens, temperature=0.3,
                )
            except Exception as e:
                last_error = e
                logger.warning(f"模型 {model_id} 失败: {e}，尝试下一个...")
        raise RuntimeError(f"所有模型均调用失败: {last_error}")

    # ── 工具执行 ──────────────────────────────────────────────

    async def _execute_tool_async(
        self,
        action: str,
        action_input: dict,
        context: AgentContext,
        tracker: ToolExecutionTracker | None = None,
        tool_use_id: str = "",
        run_id: str = "",
    ) -> str:
        """异步执行工具。优先使用 ToolRegistry，回退到 ToolNode。

        包含断路器 + 1 次失败重试。
        """
        # 权限检查
        if self._permissions and tool_use_id:
            allowed, reason = await self._permissions.check_and_wait(
                tool_use_id=tool_use_id,
                tool_name=action,
                params=action_input,
                session_id=self.session_id,
            )
            if not allowed:
                error_msg = f"权限拒绝: {reason}"
                if tracker:
                    tracker.record(action, action_input, False, error_msg, error=error_msg)
                return error_msg

        # ── 断路器检查 ──
        if not self.breaker.allow(action):
            state = self.breaker.status(action)
            cooldown_msg = (
                f"⚠️ 工具 '{action}' 暂时不可用 — "
                f"之前连续失败 {state.get('consecutive_failures', 0)} 次, "
                f"冷却剩余 {state.get('cooldown_remaining', 0)} 秒。\n"
                f"请尝试使用其他工具完成任务。"
            )
            if tracker:
                tracker.record(action, action_input, False, cooldown_msg, error="circuit_breaker_cooldown")
            return cooldown_msg

        # ── 执行（含重试）──
        max_attempts = self.tool_retry_attempts
        last_error_msg = ""

        for attempt in range(max_attempts):
            # ── 优先使用 ToolRegistry（异步）──
            if self._tool_registry and action in self._tool_registry:
                try:
                    result = await self._tool_registry.invoke(action, action_input)
                    if not result.is_error:
                        output = result.content
                        self.breaker.on_success(action)
                        if tracker:
                            tracker.record(action, action_input, True, str(output)[:200])
                        if self._trace:
                            self._trace.emit_ipc(
                                "CORE→TOOL",
                                {"tool": action, "params": action_input, "success": True},
                                run_id=run_id, kind="tool_exec",
                            )
                        return str(output)[:3000]
                    last_error_msg = f"工具执行失败: {result.content}"
                    self.breaker.on_failure(action, str(result.content))
                    if attempt < max_attempts - 1:
                        logger.warning(f"工具 {action} 失败，重试 (1/1): {str(result.content)[:100]}")
                        continue
                    if tracker:
                        tracker.record(action, action_input, False, last_error_msg, error=str(result.content))
                    if self._trace:
                        self._trace.emit_ipc(
                            "CORE→TOOL",
                            {"tool": action, "params": action_input, "success": False,
                             "error": str(result.content)},
                            run_id=run_id, kind="tool_exec",
                        )
                    # 检查断路器
                    tripped = self.breaker.on_failure_cooldown(action, last_error_msg)
                    return tripped or last_error_msg
                except Exception as e:
                    last_error_msg = f"工具执行异常: {e}"
                    logger.error(f"ToolRegistry 执行异常: {action} -> {e}")
                    self.breaker.on_failure(action, str(e))
                    if attempt < max_attempts - 1:
                        logger.warning(f"工具 {action} 异常，重试 (1/1): {e}")
                        continue
                    if tracker:
                        tracker.record(action, action_input, False, last_error_msg, error=str(e))
                    tripped = self.breaker.on_failure_cooldown(action, last_error_msg)
                    return tripped or last_error_msg

            # ── 回退到传统 ToolNode（同步）──
            tool_info = self.tools.get(action)
            if not tool_info and action in _DYNAMIC_TOOLS:
                tool_info = _DYNAMIC_TOOLS[action]

            if not tool_info:
                error_msg = f"错误: 未知工具 '{action}'"
                if tracker:
                    tracker.record(action, action_input, False, error_msg, error=error_msg)
                return error_msg

            try:
                action_input = ToolNode.normalize_params(action_input)
                node = ToolNode(f"async_react_{action}", action_type=action, **action_input)
                result = node.execute(context)

                success = result.get("success", False)
                error = result.get("error")

                if success:
                    summary = ""
                    for key in ("content", "stdout", "output", "files"):
                        if result.get(key):
                            val = result[key]
                            if isinstance(val, list):
                                summary = "\n".join(str(v) for v in val[:50])
                            else:
                                summary = str(val)[:3000]
                            break
                    if not summary:
                        summary = str(result)[:3000]
                    self.breaker.on_success(action)
                    if tracker:
                        tracker.record(action, action_input, True, summary[:200])
                    return summary
                last_error_msg = f"工具执行失败: {error or result}"
                error_str = str(error) if error else str(result)
                self.breaker.on_failure(action, error_str)
                if attempt < max_attempts - 1:
                    logger.warning(f"工具 {action} 失败，重试 (1/1): {error_str[:100]}")
                    continue
                if tracker:
                    tracker.record(action, action_input, False, last_error_msg, error=str(error))
                tripped = self.breaker.on_failure_cooldown(action, last_error_msg)
                return tripped or last_error_msg
            except Exception as e:
                last_error_msg = f"工具执行异常: {e}"
                logger.error(f"工具执行异常: {action}({action_input}) -> {e}")
                self.breaker.on_failure(action, str(e))
                if attempt < max_attempts - 1:
                    logger.warning(f"工具 {action} 异常，重试 (1/1): {e}")
                    continue
                if tracker:
                    tracker.record(action, action_input, False, last_error_msg, error=str(e))
                tripped = self.breaker.on_failure_cooldown(action, last_error_msg)
                return tripped or last_error_msg

    # ── 事件发布 ──────────────────────────────────────────────

    async def _publish_event(self, event_type: str, **kwargs: Any) -> None:
        """向 EventBus 发布事件。"""
        if not self._event_bus:
            return

        try:
            from omniagent.events.models import (
                AgentFinalAnswerEvent,
                AgentThoughtEvent,
                LlmModelSelectedEvent,
                RunErrorEvent,
                RunFinishedEvent,
                RunStartedEvent,
                RunWarningEvent,
                StepFinishedEvent,
                StepStartedEvent,
                ToolCallFinishedEvent,
                ToolCallStartedEvent,
            )

            event_map = {
                "run.started": RunStartedEvent,
                "run.finished": RunFinishedEvent,
                "step.started": StepStartedEvent,
                "step.finished": StepFinishedEvent,
                "tool.call_started": ToolCallStartedEvent,
                "tool.call_finished": ToolCallFinishedEvent,
                "llm.model_selected": LlmModelSelectedEvent,
                "agent.thought": AgentThoughtEvent,
                "agent.final_answer": AgentFinalAnswerEvent,
                "run.error": RunErrorEvent,
                "run.warning": RunWarningEvent,
            }

            event_cls = event_map.get(event_type)
            if event_cls:
                event = event_cls(**kwargs)
                await self._event_bus.publish(event)
                if self._trace:
                    self._trace.emit_event(event)
        except Exception as e:
            logger.debug(f"发布事件失败 ({event_type}): {e}")

    # ── 解析 ──────────────────────────────────────────────────

    # _parse_response 和 _input_requires_tools 从父类 ReActEngine 继承


# ═══════════════════════════════════════════════════════════════
# AsyncPlanExecuteEngine
# ═══════════════════════════════════════════════════════════════


class AsyncPlanExecuteEngine(AsyncBaseEngine):
    """异步 Plan-Execute 两阶段引擎。

    与同步版 PlanExecuteEngine 功能相同，但:
    - LLM 调用使用 chat_completion_async
    - 工具执行使用 ToolRegistry.invoke (async)
    - 集成 EventBus + TraceWriter
    """

    def __init__(
        self,
        model_priority: list[str],
        *,
        max_steps: int = 20,
        system_prompt: str | None = None,
        callback: EngineCallback | None = None,
        event_bus: Any = None,
        trace_writer: Any = None,
        tool_registry: Any = None,
        session_id: str = "",
    ) -> None:
        super().__init__(model_priority=model_priority, callback=callback)
        self.max_steps = max_steps
        self.system_prompt = system_prompt or PLAN_SYSTEM_PROMPT
        self._event_bus = event_bus
        self._trace = trace_writer
        self._tool_registry = tool_registry
        self.session_id = session_id

    async def run(self, user_input: str, context: AgentContext | None = None) -> str:
        """异步执行 Plan-Execute 流程。"""
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        ctx = context or AgentContext()
        tracker = ToolExecutionTracker()

        if self._trace:
            self._trace.open_run(run_id)

        await self._publish("run.started", run_id=run_id, goal=user_input, mode="plan-execute",
                             model_ids=self.model_priority, session_id=self.session_id)

        try:
            # Phase 1: Planning
            logger.debug("AsyncPlanExecute Phase 1: 规划中...")
            plan = await self._plan_async(user_input, ctx)
            steps = plan.get("steps", [])

            if not steps:
                self.callback.on_warning("未能生成有效的执行计划")
                await self._publish("run.finished", run_id=run_id,
                                     status="warning", result="未能生成有效的执行计划")
                return plan.get("analysis", "未能生成有效的执行计划。")

            logger.debug(f"计划生成 {len(steps)} 个步骤")
            total = min(len(steps), self.max_steps)

            # Phase 2: Execution
            logger.debug("AsyncPlanExecute Phase 2: 执行中...")
            results: list[dict[str, Any]] = []

            for i, step in enumerate(steps[:self.max_steps]):
                step_id = step.get("id", i + 1)
                step_task = step.get("task", "")
                tool = step.get("tool")
                params = step.get("params", {})

                logger.debug(f"执行步骤 {step_id}: {step_task}")
                self.callback.on_step(step_id, total, step_task)
                await self._publish("step.started", run_id=run_id, step=step_id)

                prev_results = "\n".join(
                    f"步骤 {r['step_id']}: {str(r['result'])[:200]}"
                    for r in results[-3:]
                ) if results else "(无)"

                if tool and tool != "null":
                    result = await self._execute_step_with_tool_async(
                        tool, params, ctx, tracker,
                    )
                else:
                    result = await self._execute_step_with_llm_async(
                        step_id, len(steps), step_task, prev_results, user_input, tracker,
                    )

                results.append({"step_id": step_id, "task": step_task, "result": result})
                ctx.set(f"step_{step_id}_result", result)
                success = not str(result).startswith(("执行失败", "执行异常"))
                self.callback.on_step_done(step_id, success, str(result)[:200])
                await self._publish("step.finished", run_id=run_id, step=step_id,
                                     success=success, summary=str(result)[:200])

            summary = await self._summarize_async(user_input, plan.get("analysis", ""), results,
                                                    tracker)
            await self._publish("run.finished", run_id=run_id, status="success",
                                 result=summary)
            return summary

        except asyncio.CancelledError:
            await self._publish("run.finished", run_id=run_id, status="cancelled")
            raise
        except Exception as e:
            logger.error(f"AsyncPlanExecute 异常: {e}", exc_info=True)
            await self._publish("run.error", run_id=run_id, error=str(e))
            await self._publish("run.finished", run_id=run_id, status="error", result=str(e))
            raise
        finally:
            if self._trace:
                self._trace.close_run()

    async def _plan_async(self, user_input: str, context: AgentContext | None = None) -> dict[str, Any]:
        """异步生成执行计划。"""
        messages: list[dict[str, str]] = [{"role": "system", "content": self.system_prompt}]
        self._inject_history(messages, context, max_non_system=6)
        messages.append({"role": "user", "content": user_input})

        response = await self._call_llm_async(messages)
        return parse_plan(response)

    async def _execute_step_with_tool_async(
        self, tool: str, params: dict, context: AgentContext,
        tracker: ToolExecutionTracker | None = None,
    ) -> str:
        """异步执行带工具的步骤。包含参数验证。"""
        # ── 参数验证：文件路径不能是自然语言描述 ──
        validated = _validate_tool_params(tool, params)
        if not validated["valid"]:
            error_msg = f"参数错误: {validated['reason']}"
            if tracker:
                tracker.record(tool, params, False, error_msg, error=error_msg)
            return error_msg

        try:
            params = ToolNode.normalize_params(params)
            self.callback.on_act(tool, params)

            # 优先使用 ToolRegistry
            if self._tool_registry and tool in self._tool_registry:
                result = await self._tool_registry.invoke(tool, params)
                if result.success:
                    output = result.content or str(result.data)
                    if tracker:
                        tracker.record(tool, params, True, str(output)[:200])
                    self.callback.on_observe(str(output)[:500])
                    return str(output)[:2000]
                error_detail = f"执行失败: {result.error}"
                if tracker:
                    tracker.record(tool, params, False, error_detail, error=str(result.error))
                return error_detail

            # 回退到 ToolNode
            node = ToolNode(f"plan_{tool}", action_type=tool, **params)
            result = node.execute(context)

            success = result.get("success", False)
            if success:
                summary = ""
                for key in ("content", "stdout", "output", "files"):
                    if result.get(key):
                        val = result[key]
                        summary = "\n".join(str(v) for v in val[:30]) if isinstance(val, list) else str(val)[:2000]
                        break
                if not summary:
                    summary = "执行成功"
                if tracker:
                    tracker.record(tool, params, True, summary[:200])
                self.callback.on_observe(summary)
                return summary
            error_detail = f"执行失败: {result.get('error', result)}"
            if tracker:
                tracker.record(tool, params, False, error_detail, error=str(result.get("error")))
            self.callback.on_observe(error_detail)
            return error_detail

        except Exception as e:
            error_msg = f"执行异常: {e}"
            if tracker:
                tracker.record(tool, params, False, error_msg, error=str(e))
            return error_msg

    async def _execute_step_with_llm_async(
        self, step_id: int, total: int, task: str, prev_results: str, original: str,
        tracker: ToolExecutionTracker | None = None,
    ) -> str:
        """异步执行不需要工具的步骤。"""
        prompt = EXECUTE_PROMPT.format(
            step_id=step_id, total_steps=total,
            step_task=task, previous_results=prev_results,
        )
        messages = [
            {"role": "system", "content": f"原始任务: {original}"},
            {"role": "user", "content": prompt},
        ]
        return await self._call_llm_async(messages)

    async def _summarize_async(
        self, original: str, analysis: str, results: list[dict],
        tracker: ToolExecutionTracker | None = None,
    ) -> str:
        """异步汇总所有步骤的结果。"""
        results_text = "\n".join(
            f"步骤 {r['step_id']} ({r['task']}): {str(r['result'])[:300]}"
            for r in results
        )
        tool_summary = ""
        if tracker and tracker.has_executions():
            tool_summary = f"\n\n工具执行记录:\n{tracker.detail_log()}"

        messages = [
            {"role": "system", "content": "请根据以下执行结果，给出简洁的最终总结。"},
            {"role": "user", "content": (
                f"原始任务: {original}\n\n分析: {analysis}\n\n"
                f"执行结果:\n{results_text}{tool_summary}"
            )},
        ]
        return await self._call_llm_async(messages)

    async def _publish(self, event_type: str, **kwargs: Any) -> None:
        """发布事件到 EventBus。"""
        if not self._event_bus:
            return
        try:
            from omniagent.events.models import (
                RunErrorEvent,
                RunFinishedEvent,
                RunStartedEvent,
                StepFinishedEvent,
                StepStartedEvent,
            )
            event_map: dict[str, Any] = {
                "run.started": RunStartedEvent,
                "run.finished": RunFinishedEvent,
                "run.error": RunErrorEvent,
                "step.started": StepStartedEvent,
                "step.finished": StepFinishedEvent,
            }
            event_cls = event_map.get(event_type)
            if event_cls:
                event = event_cls(**kwargs)
                await self._event_bus.publish(event)
                if self._trace:
                    self._trace.emit_event(event)
        except Exception as e:
            logger.debug(f"发布事件失败 ({event_type}): {e}")


# ═══════════════════════════════════════════════════════════════
# AsyncReflectionEngine
# ═══════════════════════════════════════════════════════════════


class AsyncReflectionEngine(AsyncBaseEngine):
    """异步 Reflection 执行-审查-修正循环引擎。

    与同步版 ReflectionEngine 功能相同，但 LLM 调用全部异步。
    """

    def __init__(
        self,
        model_priority: list[str],
        *,
        max_rounds: int = 3,
        pass_threshold: int = 7,
        executor_prompt: str | None = None,
        reviewer_prompt: str | None = None,
        callback: EngineCallback | None = None,
        event_bus: Any = None,
        trace_writer: Any = None,
        session_id: str = "",
    ) -> None:
        super().__init__(model_priority=model_priority, callback=callback)
        self.max_rounds = max_rounds
        self.pass_threshold = pass_threshold
        self.executor_prompt = executor_prompt or EXECUTOR_PROMPT
        self.reviewer_prompt = reviewer_prompt or REVIEWER_PROMPT
        self._event_bus = event_bus
        self._trace = trace_writer
        self.session_id = session_id

    async def run(self, user_input: str, context: AgentContext | None = None) -> str:
        """异步执行 Reflection 流程。"""
        run_id = f"run-{uuid.uuid4().hex[:12]}"

        if self._trace:
            self._trace.open_run(run_id)

        await self._publish("run.started", run_id=run_id, goal=user_input, mode="reflection",
                             model_ids=self.model_priority, session_id=self.session_id)

        feedback = ""
        output = ""

        try:
            for round_num in range(1, self.max_rounds + 1):
                logger.debug(f"AsyncReflection 第 {round_num} 轮")

                # Execute
                output = await self._execute_async(user_input, feedback, context)

                # Review
                review = await self._review_async(user_input, output)
                score = review.get("score", 0)
                passed = review.get("pass") and score >= self.pass_threshold

                self.callback.on_review(score, passed, review.get("feedback", "")[:200])
                await self._publish("review.finished", run_id=run_id, score=score,
                                     passed=passed, feedback=review.get("feedback", ""))

                if passed:
                    logger.debug(f"审查通过 (分数: {score})")
                    self.callback.on_finish(output)
                    await self._publish("run.finished", run_id=run_id,
                                         status="success", result=output)
                    return output

                feedback = review.get("feedback", "请改进输出质量")
                issues = review.get("issues", [])
                if issues:
                    feedback += "\n具体问题:\n" + "\n".join(f"- {i}" for i in issues)

            logger.debug(f"达到最大修正轮次 ({self.max_rounds})，返回最后一轮输出")
            self.callback.on_warning(f"达到最大修正轮次 ({self.max_rounds})")
            self.callback.on_finish(output)
            await self._publish("run.finished", run_id=run_id,
                                 status="max_rounds", result=output)
            return output

        except asyncio.CancelledError:
            await self._publish("run.finished", run_id=run_id, status="cancelled")
            raise
        except Exception as e:
            logger.error(f"AsyncReflection 异常: {e}", exc_info=True)
            await self._publish("run.error", run_id=run_id, error=str(e))
            await self._publish("run.finished", run_id=run_id, status="error", result=str(e))
            raise
        finally:
            if self._trace:
                self._trace.close_run()

    async def _execute_async(self, user_input: str, feedback: str = "",
                              context: AgentContext | None = None) -> str:
        """异步执行阶段。"""
        messages: list[dict[str, str]] = [{"role": "system", "content": self.executor_prompt}]
        self._inject_history(messages, context, max_non_system=6)
        if feedback:
            messages.append({
                "role": "user",
                "content": f"原始需求: {user_input}\n\n上一轮审查反馈:\n{feedback}\n\n请根据反馈改进你的输出。",
            })
        else:
            messages.append({"role": "user", "content": user_input})

        return await self._call_llm_async(messages)

    async def _review_async(self, user_input: str, output: str) -> dict[str, Any]:
        """异步审查阶段。"""
        messages = [
            {"role": "system", "content": self.reviewer_prompt},
            {"role": "user", "content": f"用户需求:\n{user_input}\n\n执行者输出:\n{output}"},
        ]
        response = await self._call_llm_async(messages)
        return parse_review(response)

    async def _publish(self, event_type: str, **kwargs: Any) -> None:
        """发布事件到 EventBus。"""
        if not self._event_bus:
            return
        try:
            from omniagent.events.models import (
                ReviewFinishedEvent,
                RunErrorEvent,
                RunFinishedEvent,
                RunStartedEvent,
            )
            event_map: dict[str, Any] = {
                "run.started": RunStartedEvent,
                "run.finished": RunFinishedEvent,
                "run.error": RunErrorEvent,
                "review.finished": ReviewFinishedEvent,
            }
            event_cls = event_map.get(event_type)
            if event_cls:
                event = event_cls(**kwargs)
                await self._event_bus.publish(event)
                if self._trace:
                    self._trace.emit_event(event)
        except Exception as e:
            logger.debug(f"发布事件失败 ({event_type}): {e}")
