"""异步事件驱动引擎 — 借鉴 KamaClaude 的 async event-driven loop。

将同步 ReAct/PlanExecute/Reflection 引擎改造为基于 asyncio 的异步版本，
深度集成 EventBus + TraceWriter + ToolRegistry + PermissionManagerV2。

特性:
- 异步 LLM 调用（httpx.AsyncClient）
- EventBus 发布所有生命周期事件
- 三层 Trace 记录（IPC/Event/LLM）
- 异步工具调用（ToolRegistry.invoke）
- 权限检查集成（PermissionManagerV2）
- 可取消执行（asyncio.CancelledError）
- 向后兼容（原有同步引擎不受影响）
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from omniagent.engine.callbacks import EngineCallback
from omniagent.engine.compactor import Compactor
from omniagent.engine.context import AgentContext
from omniagent.engine.tool_tracker import ToolExecutionTracker
from omniagent.nodes.tool_node import _DYNAMIC_TOOLS, ToolNode
from omniagent.utils.llm_client import chat_completion_async, chat_completion_stream_async
from omniagent.utils.response_adapter import parse_plan, parse_react, parse_review

logger = logging.getLogger(__name__)

# ── Re-import builtin tools from react_engine ─────────────────
from omniagent.engine.react_engine import BUILTIN_TOOLS, REACT_SYSTEM_PROMPT

# ── Re-import prompts from reflection_engine ──────────────────
from omniagent.engine.reflection_engine import EXECUTOR_PROMPT, REVIEWER_PROMPT

# ── Re-import prompts from plan_execute_engine ────────────────
from omniagent.engine.plan_execute_engine import EXECUTE_PROMPT, PLAN_SYSTEM_PROMPT


class AsyncReActEngine:
    """异步 ReAct 思考-行动-观察循环引擎。

    与同步版 ReActEngine 功能相同，但所有 I/O 操作都是异步的:
    - LLM 调用: chat_completion_async (httpx.AsyncClient)
    - 工具执行: ToolRegistry.invoke (async)
    - 事件发布: EventBus.publish (async)
    - Trace 记录: TraceWriter (JSONL 文件)
    """

    def __init__(
        self,
        model_priority: list[str],
        *,
        max_iterations: int = 10,
        system_prompt: str | None = None,
        tools: dict[str, dict] | None = None,
        callback: EngineCallback | None = None,
        # ── 新增: 事件/追踪/工具集成 ──
        event_bus: Any = None,       # EventBus | None
        trace_writer: Any = None,    # TraceWriter | None
        tool_registry: Any = None,   # ToolRegistry | None
        permission_manager: Any = None,  # PermissionManagerV2 | None
        session_id: str = "",
    ) -> None:
        self.model_priority = model_priority
        self.max_iterations = max_iterations
        self.tools = tools or BUILTIN_TOOLS
        self.system_prompt = system_prompt or self._build_system_prompt()
        self.callback = callback or EngineCallback()

        # 集成组件
        self._event_bus = event_bus
        self._trace = trace_writer
        self._tool_registry = tool_registry
        self._permissions = permission_manager
        self.session_id = session_id

    def _build_system_prompt(self) -> str:
        """构建包含运行环境信息的系统提示词。"""
        import sys

        tools_desc = "\n".join(
            f"- {t['name']}: {t['description']} (参数: {t['params']})"
            for t in self.tools.values()
        )

        if sys.platform == "win32":
            os_info = "Windows（使用 PowerShell 命令，不要使用 bash/Linux 命令如 ls, cat, mkdir -p, uname, which 等）"
            shell_info = "PowerShell（命令用 ; 分隔，不要用 &&）"
        elif sys.platform == "darwin":
            os_info = "macOS（使用 bash 命令）"
            shell_info = "bash/zsh"
        else:
            os_info = "Linux（使用 bash 命令）"
            shell_info = "bash"

        from datetime import datetime
        now = datetime.now()
        weekdays_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        current_datetime = f"{now.year}年{now.month}月{now.day}日 {weekdays_cn[now.weekday()]} {now.strftime('%H:%M:%S')}"

        env_info = f"""

## 运行环境

- 操作系统: {os_info}
- Shell: {shell_info}
- Python: {sys.version.split()[0]}
- 工作目录: 通过命令 `pwd`（Linux/macOS）或 `Get-Location`（Windows）获取
- 当前日期时间: {current_datetime}

重要：
- 根据操作系统使用正确的命令。Windows 下不要使用 ls, cat, mkdir -p, uname, which, grep 等 Linux 命令。
- 当用户询问日期、时间、星期几时，直接回答上面提供的当前日期时间，不要编造或猜测。
"""
        return REACT_SYSTEM_PROMPT.format(tools_desc=tools_desc) + env_info

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
        history = ctx.get_conversation_messages()
        if history:
            recent = [m for m in history if m.get("role") != "system"][-10:]
            messages.extend(recent)
            logger.debug(f"AsyncReAct 注入 {len(recent)} 条对话历史")
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
                # ── 上下文压缩检查（每 3 轮）──
                if i > 0 and i % 3 == 0:
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

                # ── Trace: LLM call ──
                if self._trace:
                    self._trace.emit_llm("CORE→LLM", model=self.model_priority[0],
                                         run_id=run_id, kind="react_iteration",
                                         data={"iteration": i + 1, "message_count": len(messages)})

                # 异步调用 LLM
                response = await self._call_llm_async(messages)

                # ── Trace: LLM response ──
                if self._trace:
                    self._trace.emit_llm("LLM→CORE", model=self.model_priority[0],
                                         run_id=run_id, kind="react_response",
                                         data={"response_len": len(response)})

                messages.append({"role": "assistant", "content": response})

                # 解析 LLM 输出
                parsed = self._parse_response(response)

                thought = parsed.get("thought", "")
                if thought:
                    self.callback.on_think(thought)
                    await self._publish_event("agent.thought", run_id=run_id, thought=thought)

                final_answer = parsed.get("final_answer", "")
                if final_answer and final_answer.strip():
                    # 验证: 如果需要工具但未执行
                    if requires_tools and not tracker.has_executions():
                        no_tool_streak += 1
                        if no_tool_streak <= 2:
                            force_msg = (
                                "⚠️ 你还没有使用任何工具就声称完成了任务。"
                                "请使用工具（如 write_file、command、create_directory 等）"
                                "实际执行操作，而不是仅在文字中描述。"
                            )
                            messages.append({"role": "user", "content": force_msg})
                            self.callback.on_warning("LLM 未执行工具就声称完成，要求重试")
                            await self._publish_event("run.warning", run_id=run_id,
                                                       warning="LLM 未执行工具就声称完成")
                            continue
                        else:
                            answer = final_answer
                            warning = (
                                "\n\n⚠️ **警告**: 本次回答未经工具执行验证。"
                                "LLM 声称完成了任务但未实际调用任何工具。"
                            )
                            self.callback.on_warning("LLM 连续拒绝工具调用，附带警告返回")
                            self.callback.on_finish(answer + warning)
                            await self._publish_event("agent.final_answer", run_id=run_id,
                                                       result=answer + warning)
                            await self._publish_event("run.finished", run_id=run_id,
                                                       status="warning", result=answer)
                            return answer + warning

                    logger.debug(f"AsyncReAct 完成，共 {i + 1} 次迭代")
                    answer = final_answer
                    self.callback.on_finish(answer)
                    await self._publish_event("agent.final_answer", run_id=run_id, result=answer)
                    await self._publish_event("run.finished", run_id=run_id,
                                               status="success", result=answer)
                    return answer

                if "action" in parsed:
                    action = parsed["action"]
                    action_input = parsed.get("action_input", {})

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

                    obs_msg = f"Observation: {observation}"
                    messages.append({"role": "user", "content": obs_msg})
                    no_tool_streak = 0

                    # ── 发布 StepFinishedEvent ──
                    await self._publish_event("step.finished", run_id=run_id, step=i + 1,
                                               success=True,
                                               summary=f"{action}: {observation[:200]}")
                else:
                    # 没有有效输出
                    last_obs = ""
                    for m in reversed(messages):
                        if m.get("role") == "user" and m.get("content", "").startswith("Observation:"):
                            last_obs = m["content"][len("Observation:"):].strip()
                            break
                    if last_obs:
                        result = last_obs[:1000]
                    else:
                        result = parsed.get("thought", "").strip() or response.strip()
                    if not result:
                        result = "任务已执行，但未生成明确的回复内容。"
                    self.callback.on_finish(result)
                    await self._publish_event("run.finished", run_id=run_id,
                                               status="completed", result=result)
                    return result

            # 达到最大迭代次数
            last_obs = ""
            for m in reversed(messages):
                if m.get("role") == "user" and m.get("content", "").startswith("Observation:"):
                    last_obs = m["content"][len("Observation:"):].strip()
                    break
            if last_obs and len(last_obs) > 50:
                msg = f"达到最大迭代次数 ({self.max_iterations})，以下是最后的执行结果：\n\n{last_obs[:2000]}"
            else:
                msg = f"达到最大迭代次数 ({self.max_iterations})，未能得出最终答案。"
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

    async def _call_llm_async(self, messages: list[dict[str, str]],
                               max_tokens: int = 131072) -> str:
        """异步调用 LLM，支持多模型 fallback。"""
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
        """异步执行工具。优先使用 ToolRegistry，回退到 ToolNode。"""
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

        # ── 优先使用 ToolRegistry（异步）──
        if self._tool_registry and action in self._tool_registry:
            try:
                result = await self._tool_registry.invoke(action, action_input)
                if result.success:
                    output = result.content or result.data.get("output", str(result.data))
                    if tracker:
                        tracker.record(action, action_input, True, str(output)[:200])
                    ## Trace: tool execution
                    if self._trace:
                        self._trace.emit_ipc(
                            "CORE→TOOL",
                            {"tool": action, "params": action_input, "success": True},
                            run_id=run_id, kind="tool_exec",
                        )
                    return str(output)[:3000]
                else:
                    error_detail = f"工具执行失败: {result.error or result.content}"
                    if tracker:
                        tracker.record(action, action_input, False, error_detail,
                                       error=str(result.error))
                    if self._trace:
                        self._trace.emit_ipc(
                            "CORE→TOOL",
                            {"tool": action, "params": action_input, "success": False,
                             "error": str(result.error)},
                            run_id=run_id, kind="tool_exec",
                        )
                    return error_detail
            except Exception as e:
                error_msg = f"工具执行异常: {e}"
                logger.error(f"ToolRegistry 执行异常: {action} -> {e}")
                if tracker:
                    tracker.record(action, action_input, False, error_msg, error=str(e))
                return error_msg

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
                    if key in result and result[key]:
                        val = result[key]
                        if isinstance(val, list):
                            summary = "\n".join(str(v) for v in val[:50])
                        else:
                            summary = str(val)[:3000]
                        break
                if not summary:
                    summary = str(result)[:3000]
                if tracker:
                    tracker.record(action, action_input, True, summary[:200])
                return summary
            else:
                error_detail = f"工具执行失败: {error or result}"
                if tracker:
                    tracker.record(action, action_input, False, error_detail, error=str(error))
                return error_detail
        except Exception as e:
            error_msg = f"工具执行异常: {e}"
            logger.error(f"工具执行异常: {action}({action_input}) -> {e}")
            if tracker:
                tracker.record(action, action_input, False, error_msg, error=str(e))
            return error_msg

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

    @staticmethod
    def _parse_response(response: str) -> dict[str, Any]:
        return parse_react(response)

    @staticmethod
    def _input_requires_tools(text: str) -> bool:
        """判断用户输入是否大概率需要工具执行。"""
        tool_keywords = [
            "文件", "文件夹", "目录", "创建", "写入", "保存", "新建", "生成",
            "读取", "查看", "修改", "编辑", "删除", "替换",
            "写", "建", "做", "搭",
            "执行", "运行", "命令", "脚本", "程序",
            "git", "commit", "push", "pull", "clone",
            "搜索", "查找", "grep", "find", "search",
            ".py", ".js", ".ts", ".html", ".css", ".json", ".yaml",
            ".md", ".txt", ".sh", ".bat",
            "src/", "test", "lib/", "app/",
            "run", "execute", "command", "script",
            "create", "write", "save",
            "read", "edit", "delete", "modify", "replace", "make", "build",
            "install", "setup",
        ]
        text_lower = text.lower()
        return any(kw in text_lower for kw in tool_keywords)


# ═══════════════════════════════════════════════════════════════
# AsyncPlanExecuteEngine
# ═══════════════════════════════════════════════════════════════


class AsyncPlanExecuteEngine:
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
        self.model_priority = model_priority
        self.max_steps = max_steps
        self.system_prompt = system_prompt or PLAN_SYSTEM_PROMPT
        self.callback = callback or EngineCallback()
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
        if context:
            history = context.get_conversation_messages()
            if history:
                recent = [m for m in history if m.get("role") != "system"][-6:]
                messages.extend(recent)
        messages.append({"role": "user", "content": user_input})

        response = await self._call_llm_async(messages)
        return parse_plan(response)

    async def _execute_step_with_tool_async(
        self, tool: str, params: dict, context: AgentContext,
        tracker: ToolExecutionTracker | None = None,
    ) -> str:
        """异步执行带工具的步骤。"""
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
                else:
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
                    if key in result and result[key]:
                        val = result[key]
                        summary = "\n".join(str(v) for v in val[:30]) if isinstance(val, list) else str(val)[:2000]
                        break
                if not summary:
                    summary = "执行成功"
                if tracker:
                    tracker.record(tool, params, True, summary[:200])
                self.callback.on_observe(summary)
                return summary
            else:
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

    async def _call_llm_async(self, messages: list[dict[str, str]],
                               max_tokens: int = 131072) -> str:
        """异步调用 LLM，支持多模型 fallback。"""
        last_error = None
        for model_id in self.model_priority:
            try:
                return await chat_completion_async(
                    model_id, messages, max_tokens=max_tokens, temperature=0.3,
                )
            except Exception as e:
                last_error = e
                logger.warning(f"模型 {model_id} 失败: {e}")
        raise RuntimeError(f"所有模型均调用失败: {last_error}")

    async def _publish(self, event_type: str, **kwargs: Any) -> None:
        """发布事件到 EventBus。"""
        if not self._event_bus:
            return
        try:
            from omniagent.events.models import (
                RunErrorEvent, RunFinishedEvent, RunStartedEvent,
                StepFinishedEvent, StepStartedEvent,
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


class AsyncReflectionEngine:
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
        self.model_priority = model_priority
        self.max_rounds = max_rounds
        self.pass_threshold = pass_threshold
        self.executor_prompt = executor_prompt or EXECUTOR_PROMPT
        self.reviewer_prompt = reviewer_prompt or REVIEWER_PROMPT
        self.callback = callback or EngineCallback()
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
        if context:
            history = context.get_conversation_messages()
            if history:
                recent = [m for m in history if m.get("role") != "system"][-6:]
                messages.extend(recent)

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

    async def _call_llm_async(self, messages: list[dict[str, str]],
                               max_tokens: int = 131072) -> str:
        """异步调用 LLM。"""
        last_error = None
        for model_id in self.model_priority:
            try:
                return await chat_completion_async(
                    model_id, messages, max_tokens=max_tokens, temperature=0.3,
                )
            except Exception as e:
                last_error = e
                logger.warning(f"模型 {model_id} 失败: {e}")
        raise RuntimeError(f"所有模型均调用失败: {last_error}")

    async def _publish(self, event_type: str, **kwargs: Any) -> None:
        """发布事件到 EventBus。"""
        if not self._event_bus:
            return
        try:
            from omniagent.events.models import (
                ReviewFinishedEvent, RunErrorEvent, RunFinishedEvent, RunStartedEvent,
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
