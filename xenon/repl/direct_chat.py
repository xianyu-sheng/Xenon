"""Direct chat path (R1-3: extracted from the kernel host mixin).

Direct-mode LLM conversation: _run_direct with its tool-need routing,
streaming/blocking responses and terminal-error classification.
Mixed into REPL as ``DirectChatMixin``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from xenon.kernel.cancel import ModelCancelled
from xenon.repl.execution_policy import ExecutionLevel, ExecutionPolicy
from xenon.repl.turn_helpers import _looks_like_external_query

logger = logging.getLogger(__name__)

# 与 repl.py 共用同一 console 实例：由 repl.py 在 Console 创建后回填。
class _ConsoleProxy:
    """动态转发到 repl.console：测试 monkeypatch repl.console 时同步生效。"""

    def __getattr__(self, name):
        from xenon.repl.repl import console

        return getattr(console, name)

    def __enter__(self):
        from xenon.repl.repl import console

        return console.__enter__()

    def __exit__(self, *exc):
        from xenon.repl.repl import console

        return console.__exit__(*exc)


console: Any = _ConsoleProxy()


@dataclass(frozen=True, slots=True)
class DirectChatOutcome:
    """What the direct path produced.

    ``delegated`` means it handed the turn to the tool path: the caller must
    not record a second, conversational ledger for the same turn.  ``run`` is
    the kernel run that did the work, and ``answer`` is that run's answer --
    a caller that cannot see the REPL console (the protocol host) still has
    something true to show instead of inventing a refusal.
    """

    answer: str = ""
    model_id: str = ""
    delegated: bool = False
    error: str = ""
    run: Any = None
    #: The operator stopped this turn.  Not an error: the caller reports an
    #: aborted turn, and no partial answer is recorded as if it were complete.
    cancelled: bool = False


def _delegated_outcome(run: Any) -> DirectChatOutcome:
    """A delegated outcome carrying the kernel run it handed the turn to."""

    result = getattr(run, "result", None)
    answer = str(getattr(result, "answer", "") or "")
    return DirectChatOutcome(delegated=True, answer=answer, run=run)


class DirectChatMixin:
    def _turn_cancel_requested(self) -> bool:
        """True when the owning front-end asked to stop the running turn.

        The interactive REPL gets Ctrl+C as ``KeyboardInterrupt`` on the main
        thread.  A protocol host runs turns on a worker thread, where the
        signal cannot land, so it installs ``_turn_cancel_probe`` and this path
        checks it between streamed chunks.  Absent a probe this is always
        False — embedded callers keep the old behaviour.
        """

        probe = getattr(self, "_turn_cancel_probe", None)
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 — a broken probe must not kill a turn
            return False

    def _escalate_to_loop(
        self, level: ExecutionLevel, reason: str
    ) -> None:
        """Raise the turn boundary before delegating a claimed action to the loop.

        Direct chat starts as ANSWER_ONLY for many chat-shaped requests.  If the
        answer claims a file operation, running the loop under the old boundary
        makes ``requires_tools`` false, so it happily accepts a no-tool answer
        (observed: the literal ``["stdio"]``).  Escalation is still bounded: the
        loop's permission gate and the user's explicit no-tools lock continue to
        decide what can execute.
        """
        try:
            current = int(self.agent_context.get("_execution_level", 0) or 0)
        except (TypeError, ValueError):
            current = 0
        target = int(level)
        if target > current:
            self.agent_context.update(
                {
                    "_execution_level": target,
                    "_execution_reason": reason,
                }
            )

    def _canonical_model_ids(self, model_ids: list[str]) -> list[str]:
        """Map registry aliases (``-m deepseek-v4-pro``) to provider/model ids.

        The kernel path already resolves aliases; the direct path handed the
        raw string to the provider layer, which then failed with "model_id 必须为
        'provider/model_name' 格式".
        """

        resolved: list[str] = []
        for raw in model_ids:
            config = self.registry.get_model(raw)
            resolved.append(config.model_id if config is not None else raw)
        return resolved

    def _run_direct(
        self,
        user_input: str,
        model_ids: list[str],
        intent: str | None = None,
        execution_policy: ExecutionPolicy | None = None,
    ) -> DirectChatOutcome:
        """直接对话模式。检测到工具需求时委派给 kernel 循环。

        分流优先级（通用设计，不枚举具体领域）：
        1. 用户明确要求只回答/不写入/不执行 → 直接回答
        2. 明确的读取、文件变更或命令任务 → 内核循环
        3. 已识别的 query 意图 → 内核循环；write_code 默认仅回答
        4. MCP 工具可用 + 输入具有"外部信息查询"特征 → 内核循环
           （通用语言结构判断，不枚举天气/高铁/酒店等具体领域）
        5. 其他 → 直接回答（纯对话/解释/闲聊）

        "直接回答"本身也是循环内的一次策略：只有明确锁定只回答时才跳过
        工具阶段，不会切换成另一个执行器。
        """
        model_ids = self._canonical_model_ids(model_ids)
        if self._turn_cancel_requested():
            # Cancelled before the first call: spend nothing on it.
            return DirectChatOutcome(cancelled=True)
        policy = execution_policy
        if policy is None:
            # 契约唯一化：入口惰性构建一次（缺失时正则回退），不再逐层重算。
            from xenon.repl.turn_contract import ensure_turn_contract

            policy = ensure_turn_contract(
                self.agent_context, user_input, fallback_intent=intent
            ).to_execution_policy()
        self.agent_context.update(
            {
                "_execution_level": int(policy.level),
                "_execution_reason": policy.reason,
            }
        )

        # 检测是否需要工具执行（文件/命令任务，或 query 意图实时数据查询）
        if policy.requires_tools:
            if intent == "query":
                console.print(
                    "[dim cyan]🔧 检测到信息查询（需实时数据），交给循环执行...[/dim cyan]"
                )
            elif intent == "research":
                console.print(
                    "[dim cyan]🔎 检测到资料调研（仅使用只读工具），交给循环执行...[/dim cyan]"
                )
            else:
                console.print(
                    "[dim cyan]🔧 检测到需要工具执行，交给循环执行...[/dim cyan]"
                )
            return _delegated_outcome(self._run_loop_turn(user_input, model_ids))

        # v0.5.3: MCP 工具可用时，通用判断——任何具有"信息查询"特征的输入都交给循环，
        # 让 LLM 自行决定是否调用 mcp_call。不枚举具体查询领域（天气/高铁/酒店等）。
        if (
            self._has_mcp_tools()
            and _looks_like_external_query(user_input)
            and not policy.locks_answer_only
        ):
            # Some short external questions do not match a prompt template.
            # MCP availability is enough to infer a read-only boundary, never
            # a write/execute authorization.
            self.agent_context.update(
                {
                    "_execution_level": int(ExecutionLevel.READ_ONLY),
                    "_execution_reason": "外部信息查询仅授权只读 MCP 工具",
                }
            )
            console.print(
                "[dim cyan]🔧 检测到可用 MCP 工具，交给循环执行...[/dim cyan]"
            )
            return _delegated_outcome(self._run_loop_turn(user_input, model_ids))

        # Direct calls also produce httpx/provider logs. Capture them while the
        # spinner is active so they cannot overwrite the Live render; Ctrl+O
        # can still reveal the captured diagnostics afterwards.
        self._last_mode_line = "· Direct 对话"
        self._last_thinking_panel = None
        self._captured_log = ""
        direct_log_chunks: list[str] = []

        messages = self.ctx_mgr.get_messages(
            include_working_memory=not self.ctx_mgr.current_request_context_frozen(),
            include_context_messages=True,
        )

        # v0.5.2: 过滤本会话已失败的模型，避免每次对话都重试不可用模型
        effective_ids = [m for m in model_ids if m not in self._failed_models]
        if not effective_ids:
            # 所有模型都已失败过——重置并重新尝试（给一次重试机会）
            self._failed_models.clear()
            effective_ids = model_ids
        elif len(effective_ids) < len(model_ids):
            skipped = set(model_ids) - set(effective_ids)
            console.print(f"[dim]· 跳过 {len(skipped)} 个已失败模型（本会话）[/dim]")

        last_error = None
        for model_id in effective_ids:
            if self._turn_cancel_requested():
                return DirectChatOutcome(cancelled=True)
            started_at = time.monotonic()
            pool_entry = self.model_pool._find_entry(model_id)
            is_retry_probe = bool(
                pool_entry is not None
                and pool_entry.health.circuit_open_until > 0
                and pool_entry.health.circuit_open_until <= started_at
            )
            try:
                self._start_log_capture()
                try:
                    if self.streaming:
                        response_text = self._stream_response(model_id, messages)
                    else:
                        response_text = self._blocking_response(model_id, messages)
                finally:
                    captured = self._stop_log_capture()
                    if captured:
                        direct_log_chunks.append(captured)
                    self._captured_log = "\n".join(direct_log_chunks)
                if not response_text or not response_text.strip():
                    raise RuntimeError(f"模型 {model_id} 返回空响应")

                if response_text:
                    # ── 响应后验证 1：检测 LLM 直接输出工具调用协议 ──
                    # v0.6.1: direct 模式不传工具定义，但 LLM 训练数据中常含
                    # {"tool": "list_files", ...} 格式。检测到后自动交给循环执行。
                    if self._detect_tool_call_json(response_text):
                        if policy.locks_answer_only:
                            raise RuntimeError(
                                "模型违反仅回答约束，返回了未执行的工具协议"
                            )
                        console.print()
                        console.print(
                            "[dim cyan]🔧 检测到 LLM 尝试调用工具但 direct 模式不可用，"
                            "交给循环执行...[/dim cyan]"
                        )
                        run = None
                        try:
                            self._escalate_to_loop(
                                ExecutionLevel.READ_ONLY,
                                "模型返回了未执行的工具协议，升级为只读重试",
                            )
                            run = self._run_loop_turn(user_input, model_ids)
                        except Exception as e:
                            console.print(f"[error]❌ 循环执行失败: {e}[/error]")
                            self._record_engine_error(
                                f"[错误] 循环执行失败: {e}", model_ids[0]
                            )
                        return _delegated_outcome(run)

                    # ── 响应后验证 2：检测 LLM 是否声称执行了文件操作 ──
                    if self._detect_file_claim(response_text):
                        if not (
                            policy.level is ExecutionLevel.ANSWER_ONLY
                            and intent == "write_code"
                        ):
                            if policy.locks_answer_only:
                                raise RuntimeError(
                                    "模型违反仅回答约束，声称执行了文件操作"
                                )
                            console.print()
                            console.print(
                                "[dim cyan]🔧 检测到 LLM 声称执行了操作但未使用工具，交给循环重新执行...[/dim cyan]"
                            )
                            # P2-修复6 (观察项-1)：防御性 catch ——
                            # _run_loop_turn 内部已加占位（修复5），但万一占位也失败
                            # （如 ctx_mgr 内部异常），这里再兜底一次防 user-only 序列。
                            run = None
                            try:
                                self._escalate_to_loop(
                                    ExecutionLevel.WRITE,
                                    "模型声称执行了文件操作，升级为可写重试",
                                )
                                run = self._run_loop_turn(user_input, model_ids)
                            except Exception as e:
                                console.print(f"[error]❌ 循环执行失败: {e}[/error]")
                                self._record_engine_error(
                                    f"[错误] 循环执行失败: {e}", model_ids[0]
                                )
                            return _delegated_outcome(run)

                    # ── 响应后验证 2：检测 LLM 是否回复了拒绝性内容 ──
                    if self._detect_denial(response_text):
                        if policy.level is ExecutionLevel.ANSWER_ONLY:
                            raise RuntimeError("模型拒绝了无需工具即可完成的请求")
                        console.print()
                        console.print("[dim]· LLM 无法完成任务 → 交给循环重试[/dim]")
                        # P2-修复6 (观察项-1)：与 file_claim 同根问题，同样防御性 catch
                        run = None
                        try:
                            self._escalate_to_loop(
                                ExecutionLevel.READ_ONLY,
                                "模型拒绝后改用工具重试，升级为只读",
                            )
                            run = self._run_loop_turn(user_input, model_ids)
                        except Exception as e:
                            console.print(f"[error]❌ 循环执行失败: {e}[/error]")
                            self._record_engine_error(
                                f"[错误] 循环执行失败: {e}", model_ids[0]
                            )
                        return _delegated_outcome(run)

                if (
                    intent == "write_code"
                    and policy.level is ExecutionLevel.ANSWER_ONLY
                ):
                    from xenon.repl.code_response import validate_code_response

                    checked = validate_code_response(user_input, response_text)
                    if not checked.valid:
                        console.print(
                            f"[dim yellow]· 代码完整性校验未通过：{checked.reason}；"
                            "正在请求同一模型重新生成[/dim yellow]"
                        )
                        retry_messages = [
                            *messages,
                            {"role": "assistant", "content": response_text},
                            {
                                "role": "user",
                                "content": (
                                    "刚才的代码未通过完整性校验。请重新输出一份完整、"
                                    "可运行的代码；只输出一个带语言标识且闭合的 Markdown "
                                    "代码块，不要调用工具，不要声称写入文件或执行命令。"
                                ),
                            },
                        ]
                        self._start_log_capture()
                        try:
                            if self.streaming:
                                response_text = self._stream_response(
                                    model_id,
                                    retry_messages,
                                )
                            else:
                                response_text = self._blocking_response(
                                    model_id,
                                    retry_messages,
                                )
                        finally:
                            captured = self._stop_log_capture()
                            if captured:
                                direct_log_chunks.append(captured)
                            self._captured_log = "\n".join(direct_log_chunks)
                        checked = validate_code_response(user_input, response_text)
                        if not checked.valid:
                            raise RuntimeError(
                                f"模型重试后代码仍不完整：{checked.reason}"
                            )
                    response_text = checked.content

                # P1-High 问题1 修复: record_model_success 内部已更新 _last_successful_model_id，
                # 状态栏通过 get_active_model_id() 自动获取，无需手动调用 set_last_model
                self.auto_router.record_model_success(model_id)
                self.model_pool.record_success(
                    model_id,
                    time.monotonic() - started_at,
                )

                # 验证完成后再持久化和渲染，避免把 DSML/JSON 伪工具调用
                # 短暂显示给用户后才交给循环执行。
                self.ctx_mgr.add_assistant_message(response_text, model_used=model_id)
                self._render_assistant_text(response_text, model_id=model_id)
                return DirectChatOutcome(answer=response_text, model_id=model_id)
            except ModelCancelled:
                # The operator stopped the turn.  Falling into the generic
                # handler would mark a healthy model as failed and silently
                # retry the next one -- the opposite of what Ctrl+C asked for.
                # Nothing partial is recorded: the answer was never delivered.
                console.print("[dim]· 已中断本轮对话（Ctrl+C）[/dim]")
                return DirectChatOutcome(cancelled=True, model_id=model_id)
            except Exception as e:
                last_error = e
                from xenon.repl.transient_errors import is_transient_error

                if is_transient_error(e):
                    self.model_pool.record_failure(
                        model_id,
                        is_retry=is_retry_probe,
                    )
                    state = "瞬时失败，尝试备用模型"
                elif self._is_terminal_model_error(e):
                    self._failed_models.add(model_id)
                    state = "配置型失败，本会话暂不重试"
                else:
                    # 未知异常先累计健康分，不因一次异常拉黑整个会话。
                    self.model_pool.record_failure(
                        model_id,
                        is_retry=is_retry_probe,
                    )
                    state = "调用失败，尝试备用模型"
                console.print(f"[dim yellow]模型 {model_id} {state}: {e}[/dim yellow]")

        error_message = f"[错误] 所有模型均调用失败: {last_error}"
        console.print(f"[error]❌ 所有模型均调用失败: {last_error}[/error]")
        # The request message was already persisted before direct execution.
        # Always close the turn after every model fails; otherwise the next
        # request creates ``user -> user`` history and the provider may answer
        # the older, unresolved prompt instead of the current one.
        self._record_engine_error(
            error_message,
            model_ids[0] if model_ids else None,
        )
        return DirectChatOutcome(error=error_message)



    @staticmethod
    def _is_terminal_model_error(error: Exception) -> bool:
        """Return True only for errors that retrying the same model cannot fix."""
        import httpx

        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code in {400, 401, 403, 404, 422}
        text = str(error).lower()
        return any(
            marker in text
            for marker in (
                "invalid api key",
                "authentication",
                "unauthorized",
                "unknown model",
                "model not found",
            )
        )



    def _stream_response(self, model_id: str, messages: list[dict[str, str]]) -> str:
        """Collect a streamed reply for validation before rendering it."""
        from xenon.utils.llm_client import chat_completion_stream

        full_response = []
        model_config = self.registry.get_model_by_id(model_id)
        request_options: dict[str, Any] = {
            "cache_lane_registry": self.ctx_mgr.prompt_lanes,
            "cache_context": {
                "engine": "direct",
                "phase": "chat",
                "context_epoch": self.ctx_mgr.cache_epoch,
                "event_cursor": self.ctx_mgr.event_cursor,
            },
        }
        if model_config:
            request_options["max_tokens"] = model_config.max_tokens
            request_options["temperature"] = model_config.temperature
            if model_config.api_key and "/" in model_id:
                request_options["credentials"] = {
                    model_id.split("/", 1)[0].lower(): model_config.api_key,
                }
            if model_config.base_url:
                request_options["base_url"] = model_config.base_url
            if model_config.reasoning_effort:
                request_options["reasoning_effort"] = model_config.reasoning_effort
        else:
            # 解析不到配置时不要静默沿用 llm_client 的 4096 默认值——那会截断
            # 长回答，用户续问时得再付一次完整 prompt 前缀的钱。
            logger.warning(
                "模型 %s 未找到运行时配置，生成预算将使用上游默认值", model_id
            )

        # 流式阶段不再绘 spinner：rich Live 会重绘整行，既干扰协议前端，
        # 也让内置编辑器的光标状态难以恢复。进度由等待提示统一负责。
        for chunk in chat_completion_stream(model_id, messages, **request_options):
            # Checked between chunks: breaking here closes the generator, which
            # releases the provider stream instead of paying for the rest of a
            # reply the user already stopped.
            if self._turn_cancel_requested():
                raise ModelCancelled("direct chat cancelled by the operator")
            full_response.append(chunk)

        response_text = "".join(full_response)

        return response_text



    def _blocking_response(self, model_id: str, messages: list[dict[str, str]]) -> str:
        """Fetch a blocking reply for validation before rendering it."""
        from xenon.utils.llm_client import chat_completion

        console.print(f"[dim]· 调用 {model_id}…[/dim]")
        model_config = self.registry.get_model_by_id(model_id)
        request_options: dict[str, Any] = {
            "cache_lane_registry": self.ctx_mgr.prompt_lanes,
            "cache_context": {
                "engine": "direct",
                "phase": "chat",
                "context_epoch": self.ctx_mgr.cache_epoch,
                "event_cursor": self.ctx_mgr.event_cursor,
            },
        }
        if model_config:
            request_options["max_tokens"] = model_config.max_tokens
            request_options["temperature"] = model_config.temperature
            if model_config.api_key and "/" in model_id:
                request_options["credentials"] = {
                    model_id.split("/", 1)[0].lower(): model_config.api_key,
                }
            if model_config.base_url:
                request_options["base_url"] = model_config.base_url
            if model_config.reasoning_effort:
                request_options["reasoning_effort"] = model_config.reasoning_effort
        else:
            logger.warning(
                "模型 %s 未找到运行时配置，生成预算将使用上游默认值", model_id
            )
        if self._turn_cancel_requested():
            raise ModelCancelled("direct chat cancelled by the operator")
        # httpx cannot abort one request from another thread, so the call runs
        # in a daemon worker while this path polls the cancel flag.  Without
        # this a protocol host (which runs turns with streaming=False) kept
        # waiting for a full reply after the user pressed Ctrl+C.
        from xenon.harness.llm import _call_with_cancel

        response = _call_with_cancel(
            self._turn_cancel_requested,
            lambda: chat_completion(model_id, messages, **request_options),
        )
        return str(response)

