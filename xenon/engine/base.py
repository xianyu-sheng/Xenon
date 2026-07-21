"""BaseEngine — 引擎抽象基类（R2）。

抽取公共属性与 ``_call_llm``，消除 react/plan/reflection/novel 四份
``_call_llm`` 复制及参数漂移：

- ``max_tokens`` 硬编码 131072 vs 8192（B4 已修，此处统一来源）；
- ``temperature`` 0.3 vs 0.8 散落各处；
- B7 的 per-model ``api_key``/``base_url`` 覆盖在 novel 中未生效（漂移 bug）。

子类只需实现 ``run`` 与自身特有参数（``max_iterations``/``max_steps``/
``max_rounds`` 等），公共 LLM 调用与多模型 fallback 由本基类提供。
"""

from __future__ import annotations

import copy
import json
import logging
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

import httpx

from xenon.engine.callbacks import EngineCallback
from xenon.engine.context import AgentContext
from xenon.repl.model_pool import FAILURE_THRESHOLD  # v0.5.3
from xenon.utils.llm_client import (
    ResponseTruncatedError,
    chat_completion,
    chat_completion_with_tools,
)

if TYPE_CHECKING:
    from xenon.engine.budget import BudgetManager
    from xenon.engine.tool_tracker import ToolExecutionTracker

logger = logging.getLogger(__name__)


class BaseEngine(ABC):
    """所有引擎的公共基类。"""

    # observation 截断阈值（子类可覆盖）；统一可配，替代各处硬编码 2000。
    observation_truncate: int = 2000

    def __init__(
        self,
        model_priority: list[str],
        *,
        callback: EngineCallback | None = None,
        model_configs: dict[str, Any] | None = None,
        temperature: float = 0.3,
        model_pool: Any = None,  # v0.4.0: ModelPool for health tracking
        auto_router: Any = None,  # v0.4.0 Step 13: AutoRouter for per-step routing
        permission_gate: Any = None,  # v0.5.0: PermissionGate for tool confirmation
    ) -> None:
        self.model_priority = list(model_priority)
        self.callback = callback or EngineCallback()
        # alias -> ModelConfig，供 _call_llm 读每模型 max_tokens/api_key/base_url（B4/B7）
        self.model_configs = dict(model_configs or {})
        # ModelRegistry stores configs by alias while engines route canonical
        # provider/model ids. Index both forms so per-model request options
        # (base URL, key, token budget, reasoning effort) actually take effect.
        for config in tuple(self.model_configs.values()):
            model_id = getattr(config, "model_id", "")
            if model_id:
                self.model_configs.setdefault(model_id, config)
        self.temperature = temperature
        self.model_pool = model_pool  # v0.4.0
        self.auto_router = auto_router  # v0.4.0 Step 13
        self.permission_gate = permission_gate  # v0.5.0
        # F6: 协作式中断标志，外部调 interrupt() 后 run() 在下一轮退出
        self._interrupted: bool = False
        # F4: 本次 run 注入的 ContextManager（run 起点设置，供 _history_messages 消费）
        self._ctx_mgr: Any = None
        # Last run's verified tool trace, exposed to the REPL for cross-turn
        # persistence. Engines without tools leave it as None.
        self._last_tracker: Any = None
        # P3-Q2: 链路追踪 ID——每次 run() 起点生成，贯穿该 run 内所有 _call_llm
        # 调用与 fallback；调试多模型失败时可把散落日志串成一条链（§8.8.4）。
        self.run_id: str | None = None
        # Native function-calling 的协议消息。pending 供当前工具执行轮消费；
        # last_provider_messages 供 REPL 按原协议持久到后续用户轮次。
        self._pending_native_response: Any = None
        self._last_provider_messages: list[dict[str, Any]] = []
        # The provider that actually completed the most recent request.  This
        # must not be inferred from model_priority[0]: fallback may succeed on
        # a later model and the REPL/status bar should report the real model.
        self.last_model_used: str | None = None

    def _begin_run(self) -> str:
        """P3-Q2: run() 起点调用——生成 run_id 并记日志，返回 run_id。

        各引擎 ``run()`` 开头调用一次，使本次运行内的所有 LLM 调用日志带同一
        ``[run_id]`` 前缀；``_call_llm`` 内每次调用再生成 ``call_id`` 细分。
        """
        from xenon.engine.trace import new_run_id, prefix
        self.run_id = new_run_id()
        self.last_model_used = None
        self._pending_native_response = None
        self._last_provider_messages = []
        logging.getLogger("xenon.engine").info(
            f"{prefix(self.run_id)} run 开始 ({type(self).__name__})")
        return self.run_id

    def interrupt(self) -> None:
        """F6: 协作式中断——外部调用后，run() 在下一轮迭代顶部退出。"""
        self._interrupted = True

    def _reset_interrupt(self) -> None:
        """每轮 run() 开头重置中断标志。"""
        self._interrupted = False

    def _resolve_model(self, step_description: str = "", count: int = 3) -> list[str]:
        """v0.4.0 Step 13: 为当前子步骤解析模型列表。

        如果 auto_router 可用且提供了步骤描述，对子任务重新路由；
        否则回退到静态 model_priority。
        """
        if self.auto_router and step_description:
            return self.auto_router.route(step_description, count=count)
        return self.model_priority

    def _context_window(self) -> int:
        """当前激活模型的上下文窗口（取最小=瓶颈模型）；未知则 128000。"""
        windows = [
            getattr(mc, "context_window", 0)
            for mc in self.model_configs.values()
            if getattr(mc, "context_window", 0) > 0
        ]
        return min(windows) if windows else 128000

    def _near_context_window(self, messages: list[dict[str, Any]], ratio: float = 0.8) -> bool:
        """F6: 估算 messages token 是否接近上下文窗口（默认 80%）。

        粗估（字符数//2）仅用于预算预警/拒绝大 observation，非精确计费。
        """
        window = self._context_window()
        if window <= 0:
            return False
        def content_size(message: dict[str, Any]) -> int:
            content = message.get("content", "")
            if isinstance(content, str):
                return len(content)
            return len(json.dumps(content, ensure_ascii=False, default=str))

        est = sum(content_size(message) for message in messages) // 2
        return est > ratio * window

    def _history_messages(
        self,
        context: Any,
        current_user_input: str | None = None,
    ) -> list[dict[str, str]]:
        """F4: 优先消费注入的 ctx_mgr（已压缩）消息，否则回退 AgentContext 历史。

        返回非 system 消息（system 由各引擎自行注入自己的 system_prompt）。
        """
        if self._ctx_mgr is not None:
            messages = [
                m for m in self._ctx_mgr.get_messages()
                if m.get("role") != "system"
            ]
            # The REPL stores the current user turn before routing. Engines add
            # that input themselves, so remove only an exact trailing duplicate.
            if (
                current_user_input is not None
                and messages
                and messages[-1].get("role") == "user"
                and messages[-1].get("content") == current_user_input
            ):
                messages.pop()
            return messages
        if context:
            return context.get_conversation_messages()
        return []

    def _working_memory_message(self) -> dict[str, str] | None:
        """Return the session's bounded working memory as a system message."""
        if self._ctx_mgr is None:
            return None
        prompt = self._ctx_mgr.working_memory_prompt()
        if not prompt:
            return None
        return {"role": "system", "content": prompt}

    def _maybe_compact_messages(
        self,
        messages: list[dict[str, Any]],
        turn: int,
        every: int = 5,
    ) -> list[dict[str, Any]]:
        """F4 + v0.5.0 P0-2: 每 ``every`` 轮压缩 in-run messages，复用 F3 + 分层策略。

        新增：压缩前对工具观察消息（"Observation: ..."）做分类压缩，
        减少工具输出占用的 prompt 空间，让 LLM 摘要更聚焦于推理链。
        """
        if turn <= 0 or turn % every != 0:
            return messages
        # ContextManager 的摘要格式不能表达 provider-issued tool_call_id。
        # 一旦存在原生工具协议消息，本轮保持原样，避免压缩后产生无效历史。
        if any(message.get("role") == "tool" or message.get("tool_calls") for message in messages):
            return messages
        try:
            from xenon.repl.context_manager import ContextManager

            # v0.5.0 P0-2：预处理工具观察消息
            preprocessed = self._preprocess_tool_observations(messages)

            tmp = ContextManager(max_tokens=self._context_window())
            for m in preprocessed:
                tmp.add_message(m.get("role", "user"), m.get("content", ""))
            tmp.compact(model_priority=self.model_priority or None)
            compacted = tmp.get_messages()
            return compacted if compacted else messages
        except Exception as e:  # noqa: BLE001 — 压缩绝不能中断主循环
            logger.warning(f"in-run 压缩失败（已忽略，沿用原 messages）: {e}")
            return messages

    @staticmethod
    def _preprocess_tool_observations(
        messages: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """v0.5.0 P0-2：对引擎内的工具观察消息做分类压缩。

        引擎层中工具结果以 "Observation: [tool_name] ..." 格式存储在
        role="user" 的消息中。此方法检测该模式并应用 ToolOutputClassifier
        压缩，减少后续 compact 的输入噪音。
        """
        import re

        # 尝试加载分类器（懒导入避免循环依赖）
        try:
            from xenon.repl.context_strategies import ToolOutputClassifier
            classifier = ToolOutputClassifier()
        except Exception:
            return messages  # 分类器不可用时原样返回

        # 匹配 "Observation: tool_name" 或 "Observation: [tool_name]"
        obs_pattern = re.compile(r"^Observation:\s*(?:\[(\w+)\]\s*)?(.*)", re.DOTALL)

        result = []
        for m in messages:
            content = m.get("content", "")
            obs_match = obs_pattern.match(content)
            if obs_match:
                tool_name = obs_match.group(1) or "unknown"
                tool_output = obs_match.group(2)
                try:
                    compressed_output = classifier.compress(tool_name, tool_output, max_chars=500)
                    result.append({
                        "role": m.get("role", "user"),
                        "content": f"Observation: [{tool_name}] {compressed_output}",
                    })
                except Exception:
                    result.append(m)  # 压缩失败，原样保留
            else:
                result.append(m)

        return result

    def _call_llm(
        self,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        *,
        model_priority: list[str] | None = None,
    ) -> str:
        """调用 LLM，支持多模型 fallback。

        ``max_tokens`` 优先级：显式入参 > ``ModelConfig.max_tokens`` > 8192 默认；
        ``chat_completion`` 再按厂商上限钳制（B4）。``api_key``/``base_url`` 按
        模型覆盖（B7）。温度取 ``self.temperature``（novel=0.8，其余=0.3）。

        ``model_priority``：可选的模型优先级覆盖（§8.23.11 / E4——Reflection 的
        reviewer 用独立模型列表，避免执行者与审查者同模型的自我审查盲区）；
        默认 None 用 ``self.model_priority``。

        错误分流（R1 / Q9）：
        - 401/403（认证失败）、400（请求被拒）= **终端错误**，切模型无意义，
          立即上抛并 ``callback.on_error``，避免用坏 Key 逐一慢试全部模型；
        - 429/5xx/网络错误/响应截断 = **瞬时错误**，切下一个模型；
        - 全部模型失败 → ``callback.on_error`` + 抛 RuntimeError。
        """
        from xenon.engine.trace import new_call_id, prefix
        call_id = new_call_id()

        def tp(message: str) -> str:
            return f"{prefix(self.run_id, call_id)} {message}"
        last_error: Exception | None = None
        for model_id in (model_priority or self.model_priority):
            started_at = time.monotonic()
            try:
                if self.model_pool:
                    self.model_pool.acquire(model_id)  # P2: 并发计数+1(资源感知)
                mc = self.model_configs.get(model_id)
                mt = max_tokens or getattr(mc, "max_tokens", None) or 8192
                creds = None
                base = None
                if mc:
                    base = getattr(mc, "base_url", "") or None
                    mk = getattr(mc, "api_key", "") or ""
                    if mk and "/" in model_id:
                        creds = {model_id.split("/", 1)[0].lower(): mk}
                logger.debug(tp(f"调用模型 {model_id}"))
                request_options: dict[str, Any] = {
                    "max_tokens": mt,
                    "temperature": self.temperature,
                    "credentials": creds,
                    "base_url": base,
                }
                effort = getattr(mc, "reasoning_effort", "") if mc else ""
                if effort:
                    request_options["reasoning_effort"] = effort
                result = chat_completion(model_id, messages, **request_options)
                # v0.4.0: record success to model pool
                if self.model_pool:
                    self.model_pool.record_success(
                        model_id,
                        time.monotonic() - started_at,
                    )
                self.last_model_used = model_id
                return result
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (401, 403):
                    self.callback.on_error(
                        f"模型 {model_id} 认证失败 ({status})，请检查 API Key")
                    raise RuntimeError(
                        f"模型 {model_id} 认证失败 ({status})，请检查 API Key") from e
                if status == 400:
                    self.callback.on_error(f"模型 {model_id} 请求被拒 (400): {e}")
                    raise RuntimeError(
                        f"模型 {model_id} 请求被拒 (400)，请检查参数/模型名") from e
                # 429/5xx/其他 HTTP：瞬时，切下一个模型
                if self.model_pool:
                    self._record_model_failure(model_id)
                last_error = e
                logger.warning(tp(f"模型 {model_id} HTTP {status} 失败: {e}，尝试下一个..."))
            except (
                httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
                httpx.RemoteProtocolError, httpx.WriteError, httpx.PoolTimeout,
            ) as e:
                if self.model_pool:
                    self._record_model_failure(model_id)
                last_error = e
                logger.warning(tp(f"模型 {model_id} 网络错误 ({type(e).__name__}): {e}，尝试下一个..."))
            except ResponseTruncatedError as e:
                if self.model_pool:
                    self._record_model_failure(model_id)
                last_error = e
                logger.warning(tp(f"模型 {model_id} 响应截断: {e}，尝试下一个..."))
            except Exception as e:
                if self.model_pool:
                    self._record_model_failure(model_id)
                last_error = e
                logger.warning(tp(f"模型 {model_id} 失败: {e}，尝试下一个..."))
            finally:
                # P2: 释放并发计数(无论成败)
                if self.model_pool:
                    self.model_pool.release(model_id)
        # P2: 限流退避--全链瞬时失败时退避后重试链首,而非立即上抛
        # (避免 ReAct 第 N 步 RPM 限流中断,浪费前 N-1 步 token)
        if self._is_transient_error(last_error) and getattr(self, '_call_retry_depth', 0) < 2:
            self._call_retry_depth = getattr(self, '_call_retry_depth', 0) + 1
            try:
                wait = self._extract_retry_after(last_error, default=2.0 * self._call_retry_depth)
                logger.warning(tp(
                    f"全链瞬时失败({type(last_error).__name__}),"
                    f"退避 {wait:.1f}s 后重试(depth {self._call_retry_depth}/2)"))
                import time as _t
                _t.sleep(wait)
                return self._call_llm(messages, max_tokens, model_priority=model_priority)
            finally:
                self._call_retry_depth -= 1
        self.callback.on_error(f"所有模型均调用失败: {last_error}")
        raise RuntimeError(f"所有模型均调用失败: {last_error}")

    def _record_model_failure(self, model_id: str) -> None:
        """Record a failed half-open probe without confusing it with a first failure."""
        if not self.model_pool:
            return
        entry = self.model_pool._find_entry(model_id)
        now = time.monotonic()
        is_retry = bool(
            entry is not None
            and entry.health.consecutive_failures >= FAILURE_THRESHOLD
            and entry.health.circuit_open_until > 0
            and entry.health.circuit_open_until <= now
        )
        self.model_pool.record_failure(model_id, is_retry=is_retry)

    # ── P2: 限流退避辅助 ─────────────────────────────────

    @staticmethod
    def _is_transient_error(e: Exception | None) -> bool:
        """判断错误是否值得退避重试(瞬时错误)。终端错误(401/403/400)不会到达此处。"""
        if e is None:
            return False
        if isinstance(e, ResponseTruncatedError):
            return True
        if isinstance(e, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
                          httpx.RemoteProtocolError, httpx.WriteError, httpx.PoolTimeout)):
            return True
        if isinstance(e, httpx.HTTPStatusError):
            status = e.response.status_code
            return status == 429 or 500 <= status < 600
        return False  # 未知异常保守不退避

    @staticmethod
    def _extract_retry_after(e: Exception | None, default: float = 2.0) -> float:
        """从 429 响应头取 Retry-After,否则返回 default(上限 30s 防长阻塞)。"""
        if isinstance(e, httpx.HTTPStatusError):
            try:
                ra = e.response.headers.get("retry-after")
                if ra:
                    return min(float(ra), 30.0)
            except Exception:
                pass
        return default

    # ── F5: 三层 LLM 降级 _call_llm_native ───────────────────

    def _call_with_tools_once(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None,
        response_format: dict[str, Any] | None,
        max_tokens: int | None = None,
    ) -> Any:
        """单层 native FC 调用，遍历 ``model_priority``。

        返回 ``LLMResponse`` 或 ``None``（本层降级信号）。

        错误分流（与 ``_call_llm`` 一致 + 降级语义）：
        - 401/403 = 终端错误，立即上抛（认证坏 Key 切模型无意义）；
        - 400 = 该模型可能**不支持 tools/response_format** → 试下一个模型，
          全部 400 则本层降级（返回 None），让外层切到下一层 tier；
        - 429/5xx/网络/截断 = 瞬时 → 试下一个模型，全败则本层降级。
        """
        last_error: Exception | None = None
        for model_id in self.model_priority:
            try:
                mc = self.model_configs.get(model_id)
                mt = max_tokens or getattr(mc, "max_tokens", None) or 4096
                creds = None
                base = None
                if mc:
                    base = getattr(mc, "base_url", "") or None
                    mk = getattr(mc, "api_key", "") or ""
                    if mk and "/" in model_id:
                        creds = {model_id.split("/", 1)[0].lower(): mk}
                request_options: dict[str, Any] = {
                    "tools": tools,
                    "response_format": response_format,
                    "credentials": creds,
                    "base_url": base,
                    "max_tokens": mt,
                    "temperature": self.temperature,
                }
                effort = getattr(mc, "reasoning_effort", "") if mc else ""
                if effort:
                    request_options["reasoning_effort"] = effort
                response = chat_completion_with_tools(
                    model_id, messages, **request_options,
                )
                self.last_model_used = model_id
                return response
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (401, 403):
                    self.callback.on_error(
                        f"模型 {model_id} 认证失败 ({status})，请检查 API Key")
                    raise RuntimeError(
                        f"模型 {model_id} 认证失败 ({status})，请检查 API Key") from e
                # 400（不支持 tools/format）/ 429 / 5xx：试下一个模型
                last_error = e
                logger.warning(
                    f"模型 {model_id} native 调用 HTTP {status}: {e}，尝试下一个...")
            except (
                httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
                httpx.RemoteProtocolError, httpx.WriteError, httpx.PoolTimeout,
            ) as e:
                last_error = e
                logger.warning(f"模型 {model_id} 网络错误 ({type(e).__name__}): {e}，尝试下一个...")
            except Exception as e:  # noqa: BLE001 — 本层降级，不中断
                last_error = e
                logger.warning(f"模型 {model_id} native 调用失败: {e}，尝试下一个...")
        logger.warning(f"_call_with_tools_once 本层全败 ({last_error})，降级")
        return None

    @staticmethod
    def _tool_calls_to_react_json(tool_calls: list[dict[str, Any]]) -> str:
        """把原生 tool_calls 合成 ReAct JSON 串，供 ``parse_react`` 统一解析。

        v0.5.0: 多工具调用 → 返回 JSON 数组；单工具 → 返回单个 JSON 对象。
        parse_react 会按类型自动分流：dict 单工具，list 并行工具。
        """
        import json

        actions = []
        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("arguments", {}) or {}
            if not name:
                continue
            actions.append({"thought": "", "action": name, "action_input": args})

        if not actions:
            return json.dumps(
                {"thought": "", "action": "", "action_input": {}},
                ensure_ascii=False,
            )
        if len(actions) == 1:
            return json.dumps(actions[0], ensure_ascii=False)
        return json.dumps(actions, ensure_ascii=False)

    # ── v0.5.0: 并行工具执行 ───────────────────────────────

    # 无副作用、可安全并行的工具类型
    _PARALLEL_SAFE_TOOLS: frozenset[str] = frozenset({
        "read_file", "search_files", "list_files",
        "code_index", "ast_analyze", "web_fetch",
        "github_fetch", "weather", "datetime",
    })

    def _execute_tools_parallel(
        self,
        actions: list[dict[str, Any]],
        context: Any,
        tracker: Any,
        max_workers: int = 5,
    ) -> list[tuple[dict[str, Any], str]]:
        """并行执行多个工具调用。

        使用 ThreadPoolExecutor（与 plan_dag.py 一致），
        单个工具失败不影响其他并行工具。

        Returns:
            [(action_dict, observation_str), ...] — 保持原始顺序
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: dict[int, str] = {}

        def _exec_one(idx: int, action: dict) -> tuple[int, str]:
            tool_name = action.get("action", "")
            params = action.get("action_input", {})

            # 有副作用的工具强制串行（标记但由调用方决定是否真正并行）
            try:
                obs = self._execute_tool(tool_name, params, context, tracker)
            except Exception as e:
                obs = f"⛔ 工具 {tool_name} 执行异常: {e}"
            return idx, obs

        if len(actions) <= 1:
            # 单工具：直接执行
            for i, a in enumerate(actions):
                _, obs = _exec_one(i, a)
                results[i] = obs
        else:
            with ThreadPoolExecutor(max_workers=min(max_workers, len(actions))) as pool:
                futures = {pool.submit(_exec_one, i, a): i for i, a in enumerate(actions)}
                for future in as_completed(futures):
                    try:
                        idx, obs = future.result()
                        results[idx] = obs
                    except Exception as e:
                        idx = futures[future]
                        results[idx] = f"⛔ 工具执行异常: {e}"

        # 保持原始顺序
        return [(actions[i], results.get(i, "⛔ 未执行")) for i in range(len(actions))]

    def _execute_tool(
        self, tool_name: str, params: dict[str, Any],
        context: Any, tracker: Any,
    ) -> str:
        """执行单个工具并返回观察文本。

        子类（如 ReActEngine）应重写此方法以使用 ToolExecutor 流水线。
        默认实现返回占位文本。
        """
        return f"[工具 {tool_name} 未实现]"

    def _call_llm_native(
        self,
        messages: list[dict[str, str]],
        tools_schema: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """F5: 三层 LLM 降级（依赖 R3 ``chat_completion_with_tools``）。

        ① **Native FC + 结构化输出**：``tools + response_format``——模型直接返回
          原生 ``tool_calls``（最可靠，无 JSON 解析风险）；
        ② **tools only**：去 ``response_format``，只传 tools + ``parse_react``——
          部分模型不支持 response_format 但支持 tools；
        ③ **schema only**：只传 ``response_format`` 不传 tools——模型不识别原生
          tools 时退化为 JSON 模式文本，由 ``parse_react`` 解析；
        三层全败回退 ``_call_llm``（纯文本 + ``parse_react``，引擎现状最低层）。

        返回字符串：tier①/② 拿到原生 ``tool_calls`` 时合成 ReAct JSON，否则返回
        ``content``（由调用方 ``parse_react``）。无 ``tools_schema`` 且无
        ``response_format`` 时直接回退 ``_call_llm``。
        """
        self._pending_native_response = None
        if not tools_schema and not response_format:
            return self._call_llm(messages, max_tokens=max_tokens)

        tiers = [
            ("tier1_tools+format", tools_schema, response_format),
            ("tier2_tools_only", tools_schema, None),
            ("tier3_format_only", None, response_format),
        ]
        # 过滤掉 tools/format 都没有的空层（避免与 fallback 重复）
        tiers = [(n, t, f) for n, t, f in tiers if (t or f)]

        for tier_name, tools, fmt in tiers:
            resp = self._call_with_tools_once(messages, tools, fmt, max_tokens)
            if resp is None:
                continue  # 本层降级，试下一层
            if resp.has_tool_calls:
                logger.info(f"_call_llm_native {tier_name} 拿到原生 tool_calls")
                self._pending_native_response = resp
                return self._tool_calls_to_react_json(resp.tool_calls)
            if resp.content and resp.content.strip():
                logger.info(f"_call_llm_native {tier_name} 返回文本（parse_react）")
                return resp.content
            logger.warning(f"_call_llm_native {tier_name} 返回空，降级下一层")

        logger.warning("_call_llm_native 三层全败，回退 _call_llm")
        return self._call_llm(messages, max_tokens=max_tokens)

    def _has_pending_native_tool_calls(self) -> bool:
        """当前响应是否包含尚未写回历史的原生工具调用。"""
        response = self._pending_native_response
        return bool(response is not None and getattr(response, "has_tool_calls", False))

    def _consume_native_tool_messages(
        self,
        observations: list[str],
    ) -> list[dict[str, Any]]:
        """生成可继续调用 DeepSeek/OpenAI 的完整工具协议消息。

        DeepSeek V4 思考模式要求工具调用后的请求带回 assistant 的
        ``reasoning_content``、``tool_calls`` 以及逐个 ``tool_call_id`` 对应的
        tool result。这里使用 API 原始 assistant message，并只补齐缺失字段。
        """
        response = self._pending_native_response
        self._pending_native_response = None
        if response is None or not getattr(response, "has_tool_calls", False):
            return []

        tool_calls = list(getattr(response, "tool_calls", []) or [])
        if len(tool_calls) != len(observations):
            logger.warning(
                "原生工具调用与观察结果数量不一致 (%s != %s)，回退普通观察消息",
                len(tool_calls),
                len(observations),
            )
            return []

        assistant = copy.deepcopy(getattr(response, "assistant_message", None) or {})
        assistant["role"] = "assistant"
        assistant["content"] = assistant.get("content") or ""
        if getattr(response, "reasoning_content", "") and not assistant.get("reasoning_content"):
            assistant["reasoning_content"] = response.reasoning_content
        if not assistant.get("tool_calls"):
            assistant["tool_calls"] = [
                {
                    "id": call.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": call.get("name", ""),
                        "arguments": json.dumps(
                            call.get("arguments", {}) or {}, ensure_ascii=False
                        ),
                    },
                }
                for call in tool_calls
            ]

        protocol_messages: list[dict[str, Any]] = [assistant]
        for call, observation in zip(tool_calls, observations):
            protocol_messages.append({
                "role": "tool",
                "tool_call_id": str(call.get("id", "")),
                "content": observation,
            })

        self._last_provider_messages.extend(copy.deepcopy(protocol_messages))
        return protocol_messages

    # ── F2: 合成提示注入 ─────────────────────────────────────

    def _inject_synthesis_prompt(
        self,
        budget: BudgetManager,
        tracker: ToolExecutionTracker | None,
    ) -> tuple[str, str] | None:
        """F2: 按剩余预算/工具调用/阶段选择合成提示场景（6 场景）。

        返回 ``(scenario, prompt)`` 或 ``None``（无需注入）。调用方把 ``prompt``
        作为 user 消息追加进 ``messages``，引导 LLM 在当前阶段做正确的事。

        场景优先级：
        1. **force_synthesis**：剩余预算 <15% 且有工具调用 → 必须立即合成最终答案；
        2. （刚奖励过空洞补救 → 跳过，hint 已注入，避免连续 user 消息堆叠）；
        3. **converge_synthesis**：收束阶段且有工具调用 → 准备收尾合成；
        4. **soft_warning**：收束阶段但 0 工具调用 → 立即行动或基于已知回答；
        5. **compression_reward**：刚触发压缩奖励 → 鼓励继续产出；
        6. **progress_expansion**：中段执行且最近成功 → 进展良好继续；
        7. **gentle_hint**：探索阶段且 0 工具调用 → 2-3 步后开始执行。
        """
        tool_calls = len(tracker.calls) if tracker else 0
        last_success = bool(tracker and tracker.calls and tracker.calls[-1].success)
        total = budget.total if budget.total > 0 else 1
        remaining_ratio = budget.remaining / total

        # 1. 强制合成：预算将尽且做过工
        if remaining_ratio < 0.15 and tool_calls >= 1:
            return (
                "force_synthesis",
                f"⚠️ 预算仅剩 {budget.remaining}/{budget.total} 轮，你已执行 {tool_calls} 次工具。"
                "必须在本轮直接给出 final_answer——基于已执行的工具结果合成最终回答，"
                "不要再调用工具，直接总结产物（文件路径/代码/命令输出）。",
            )

        # 2. 刚奖励过空洞补救：hint 已作为上一条 user 消息注入，跳过避免堆叠
        if budget.rewards and budget.rewards[-1][0] == "hollow":
            return None

        # 3. 收束阶段且有工具：准备合成
        if budget.is_converge_phase() and tool_calls >= 1:
            return (
                "converge_synthesis",
                f"ℹ️ 已进入收束阶段（{budget.summary()}），已执行 {tool_calls} 次工具。"
                "请停止探索，基于已有结果整理 final_answer，附上产物路径/代码/命令输出。",
            )

        # 4. 收束阶段但没工具：立即行动
        if budget.is_converge_phase() and tool_calls == 0:
            return (
                "soft_warning",
                "⚠️ 已进入收束阶段但未调用任何工具。请立即调用工具执行，"
                "或基于已知信息直接给出 final_answer，不要再探索。",
            )

        # 5. 压缩奖励：鼓励继续
        if budget.rewards and budget.rewards[-1][0] == "compression":
            n = budget.rewards[-1][1]
            return (
                "compression_reward",
                f"ℹ️ 上下文已压缩，奖励 +{n} 轮预算。把省下的预算用在产出上，继续执行剩余任务。",
            )

        # 6. 中段执行良好：鼓励
        if budget.is_execute_phase() and tool_calls >= 3 and last_success:
            return (
                "progress_expansion",
                f"✓ 进展良好（{tool_calls} 次工具，最近一次成功）。"
                "继续执行剩余步骤，完成后给出 final_answer。",
            )

        # 7. 探索阶段无工具：温和提示
        if budget.is_explore_phase() and tool_calls == 0:
            return (
                "gentle_hint",
                "ℹ️ 当前为探索阶段。建议 2-3 步了解结构后立即开始执行（write_file/command），"
                "不要无限探索。",
            )

        return None

    # ── F2: mercy compile / exhaustion report ────────────────

    def _synthesis_prompt(self, user_input: str, tracker: ToolExecutionTracker) -> str:
        """构造 mercy compile 的无格式约束合成 prompt。"""
        return (
            "你是一个 Agent 的收尾合成器。Agent 已执行若干工具但未在预算内给出最终答案。\n"
            f"用户原始需求：{user_input}\n\n"
            f"已执行工具记录：\n{tracker.detail_log()}\n\n"
            "请基于以上工具执行结果，直接给出最终回答——给用户看的自然语言总结，"
            "附上产物路径/代码/命令输出。不要输出 JSON，不要 ReAct 格式，直接回答。"
        )

    def _exhaustion_report(self, user_input: str, tracker: ToolExecutionTracker) -> str:
        """F2: 从 tracker.calls 程序化拼出结构化报告（成功/失败/参数/最多 10 条）。"""
        lines = [
            "⚠️ 达到最大迭代次数，以下是已执行工具的结构化报告：",
            "",
            f"**用户需求**：{user_input}",
            "",
            f"**执行摘要**：{tracker.execution_summary()}",
            "",
            "**详细记录**（最多 10 条）：",
        ]
        for i, call in enumerate(tracker.calls[-10:], 1):
            status = "✓ 成功" if call.success else "✗ 失败"
            params = call.params or {}
            lines.append(f"{i}. {status} {call.tool_name}({params})")
            if call.result_summary:
                lines.append(f"   结果：{call.result_summary}")
            if call.error:
                lines.append(f"   错误：{call.error}")
        lines.append("")
        lines.append("请基于以上执行结果判断任务完成度，或重新发起更具体的指令。")
        return "\n".join(lines)

    def _mercy_compile(
        self,
        user_input: str,
        tracker: ToolExecutionTracker | None,
        messages: list[dict[str, str]],
    ) -> str:
        """F2: 迭代耗尽时的优雅降级链（mercy compile → exhaustion report → 报错）。

        ① 换备选模型做一次**无 ReAct 格式约束**的合成（仅当有工具执行数据）；
        ② 合成失败/无数据则从 ``tracker.calls`` 程序化拼出结构化报告；
        ③ 连工具数据都没有才报错。

        避免 §8.x 的"一次瞬时 API 故障直接杀掉整个运行"——``tracker.calls`` 数据
        在手却未用，这里把它变成可用的部分结果。
        """
        # ① 备选模型合成（有工具数据才值得合成）
        if tracker and tracker.has_executions():
            try:
                answer = self._call_llm([
                    {"role": "system",
                     "content": "你是 Agent 的收尾合成器，直接输出最终回答，不要 JSON/ReAct 格式。"},
                    {"role": "user", "content": self._synthesis_prompt(user_input, tracker)},
                ])
                if answer and answer.strip():
                    self.callback.on_warning("迭代耗尽，已用 LLM 合成最终回答（mercy compile）")
                    return answer.strip()
            except Exception as e:  # noqa: BLE001 — 合成失败回退报告，不抛
                logger.warning(f"mercy compile 合成失败，回退结构化报告: {e}")
            # ② 结构化报告
            self.callback.on_warning("迭代耗尽，已生成结构化执行报告（exhaustion report）")
            return self._exhaustion_report(user_input, tracker)

        # ③ 无数据
        self.callback.on_error("迭代耗尽且无工具执行数据，无法合成结果")
        max_iter = getattr(self, "max_iterations", None)
        budget_str = f" ({max_iter}) " if max_iter else " "
        return (
            f"达到最大迭代次数{budget_str}未能得出最终答案，"
            "且未执行任何工具调用。请尝试简化问题或使用更具体的指令。"
        )

    @abstractmethod
    def run(self, user_input: str, context: AgentContext | None = None) -> str:
        """子类实现主循环。"""
        raise NotImplementedError
