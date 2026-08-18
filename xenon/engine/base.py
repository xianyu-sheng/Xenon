"""BaseEngine — 引擎抽象基类（R2）。

抽取公共属性与 ``_call_llm``，消除执行引擎之间的重复实现
``_call_llm`` 复制及参数漂移：

- ``max_tokens`` 硬编码 131072 vs 8192（B4 已修，此处统一来源）；
- ``temperature`` 0.3 vs 0.8 散落各处；
- B7 的 per-model ``api_key``/``base_url`` 覆盖统一由基类生效。

子类只需实现 ``run`` 与自身特有参数（``max_iterations``/``max_steps``/
``max_rounds`` 等），公共 LLM 调用与多模型 fallback 由本基类提供。
"""

from __future__ import annotations

import copy
import json
import logging
import queue
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

import httpx

from xenon.engine.callbacks import EngineCallback
from xenon.engine.context import AgentContext
from xenon.engine.execution_policy import (
    DEFAULT_ENGINE_TIMEOUT,
    EngineDeadlineExceeded,
    ExecutionPolicy,
)
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
        # Mid-task steering：任务运行期间用户补充/修改要求的通道。
        # 与 F6 中断互补——interrupt() 是「退出」，steer() 是「转向」：
        # 消息先入队，引擎在下一个迭代检查点消费，当前工具调用不被打断
        # （避免副作用中途掐断留下脏状态）。7 个引擎全部在 BaseEngine
        # 继承该机制，不各自实现。
        self._steering_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        # 本次 run 已消费的 steering 消息（供 REPL 展示/测试断言）
        self.steering_consumed: list[dict[str, Any]] = []
        # F4: 本次 run 注入的 ContextManager（run 起点设置，供 _history_messages 消费）
        self._ctx_mgr: Any = None
        # Last run's verified tool trace, exposed to the REPL for cross-turn
        # persistence. Engines without tools leave it as None.
        self._last_tracker: Any = None
        # P3-Q2: 链路追踪 ID——每次 run() 起点生成，贯穿该 run 内所有 _call_llm
        # 调用与 fallback；调试多模型失败时可把散落日志串成一条链（§8.8.4）。
        self.run_id: str | None = None
        self.evidence_ledger: Any = None
        # Native function-calling 的协议消息。pending 供当前工具执行轮消费；
        # last_provider_messages 供 REPL 按原协议持久到后续用户轮次。
        self._pending_native_response: Any = None
        self._last_provider_messages: list[dict[str, Any]] = []
        # Session-local protocol capability memory.  A 400 from one native
        # request shape is stable compatibility evidence; retrying the same
        # tools+format shape every ReAct iteration wastes RPM and can hide the
        # actual task result behind provider limits.
        self._unsupported_native_shapes: set[
            tuple[tuple[str, ...], bool, bool]
        ] = set()
        # v0.8.3: native 请求整体失败（5xx/网络断连全模型）后熔断——本 run
        # 内直接走文本协议（_call_llm 带 chain_retries 重试 + 模型池切换），
        # 避免每个 ReAct 迭代都重复 native 失败请求。SWE-bench 实测：
        # react 4 例因 "native provider request failed" 直接挂掉整个单元格，
        # 而文本协议 + parse_react 完全有能力完成同一任务。
        self._native_request_failed = False
        # The provider that actually completed the most recent request.  This
        # must not be inferred from model_priority[0]: fallback may succeed on
        # a later model and the REPL/status bar should report the real model.
        self.last_model_used: str | None = None
        self._active_cache_phase: str = "request"
        # Exactly one retry layer owns transient retries.  Benchmark adapters
        # replace this unbounded interactive policy with one absolute deadline
        # shared by the whole engine graph.
        self.execution_policy = ExecutionPolicy.from_timeout(
            DEFAULT_ENGINE_TIMEOUT,
            request_timeout=120.0,
            chain_retries=2,
        )
        # EvidenceGate 管线：竖着贯穿会话生命周期（fact → plan → completion →
        # fix → output），每层确定性校验、层层过滤。引擎可通过 register_gate
        # 追加专用 Gate；默认管线在基类统一挂载，避免各引擎漏接。
        from xenon.engine.evidence_gate import default_gates

        self._gates: list[Any] = list(default_gates())

        # v0.8.3: 引擎层跨轮次验证循环组件（可选，默认 None = 不启用）。
        # 子引擎在 __init__ 中创建并赋值，或由外部注入。
        self.verification_loop: Any = None

    def register_gate(self, gate: Any) -> None:
        """挂载一个 EvidenceGate 到本引擎的会话级管线。"""

        self._gates.append(gate)

    def run_gates(
        self,
        phase: str,
        ctx: AgentContext | None = None,
        **kwargs: Any,
    ) -> list[Any]:
        """运行指定 phase 的所有 Gate，返回 GateVerdict 列表（按注册序）。

        确定性、零 LLM；调用方（引擎 run 流程）根据 verdict 决定补救动作。
        """

        return [
            gate.check(ctx, **kwargs)
            for gate in self._gates
            if getattr(gate, "phase", None) == phase
        ]

    def gate_failed(
        self,
        phase: str,
        ctx: AgentContext | None = None,
        **kwargs: Any,
    ) -> Any | None:
        """运行指定 phase 的 Gate，返回第一个未通过的 verdict；全通过返回 None。"""
        for verdict in self.run_gates(phase, ctx, **kwargs):
            if not verdict.passed:
                return verdict
        return None

    def gate_warning(
        self,
        phase: str,
        ctx: AgentContext | None = None,
        **kwargs: Any,
    ) -> str | None:
        """运行 Gate 并把拒绝转换为可观测 warning；不调用 LLM、不阻断交付。"""
        verdict = self.gate_failed(phase, ctx, **kwargs)
        if verdict is None:
            return None
        message = f"{phase} EvidenceGate: {verdict.reason}"
        logging.getLogger("xenon.engine").warning(message)
        return message

    def _bind_evidence_ledger(self, context: AgentContext) -> Any:
        """Create and bind one ledger to the current task context.

        ToolExecutor resolves this Context key, so every engine/tool call within
        the task appends to the same hash chain rather than a private audit log.
        If the context already carries a runtime (e.g. the REPL started the task
        first), reuse its ledger so task intake and engine events stay in one chain.
        """
        from xenon.engine.evidence_runtime import (
            EvidenceLedger, EvidenceSource, EventKind, LifecyclePhase,
        )
        existing = context.get_evidence_runtime()
        if existing is not None:
            ledger = existing.ledger
            self.evidence_ledger = ledger
            context.set("_evidence_session_id", ledger.session_id)
            context.set("_evidence_ledger", ledger)
            return ledger
        if self.run_id is None:
            self._begin_run()
        ledger = EvidenceLedger(self.run_id or "unknown")
        ledger.append(
            LifecyclePhase.TASK, EventKind.TASK_FACT, EvidenceSource.ENGINE,
            {"engine": type(self).__name__, "run_id": self.run_id},
        )
        context.set("_evidence_session_id", self.run_id)
        context.set("_evidence_ledger", ledger)
        context.bind_evidence(ledger)
        self.evidence_ledger = ledger
        return ledger

    def finalize_evidence(
        self,
        *,
        context: AgentContext | None = None,
        output: str = "",
        tracker: Any | None = None,
        workspace_root: Any = None,
    ) -> Any:
        """Build the delivery EvidencePack only after final deterministic gates pass."""
        from xenon.engine.evidence_runtime import EvidencePack, EvidenceSource, EventKind, LifecyclePhase
        ctx = context or AgentContext()
        ledger = self.evidence_ledger or ctx.get("_evidence_ledger")
        if ledger is None:
            raise RuntimeError("evidence ledger is not bound to this task")
        for phase, kwargs in (
            ("fix", {"tracker": tracker}),
            ("output", {"output": output, "tracker": tracker, "workspace_root": workspace_root}),
        ):
            verdict = self.gate_failed(phase, ctx, **kwargs)
            ledger.append(
                LifecyclePhase.DELIVERY, EventKind.GATE_VERDICT, EvidenceSource.GATE,
                {"phase": phase, "passed": verdict is None,
                 "reason": verdict.reason if verdict else "passed"},
            )
            if verdict is not None:
                # SWE-bench 实测回归（sphinx-7738、plan-reflection 6 例、
                # react-reflection 3 例）：修复已通过写工具真正落盘（patch
                # 已产出），但 LLM 总结里声称的辅助文件（复现脚本等）未
                # 经工具验证。此前此处无条件 raise，直接把已完成的成果
                # 整体丢弃。分层：任务已落盘 → 门失败降级为已记录警告，
                # 产出保留；任务从未落盘 → 维持 fail-closed（防「贴 diff
                # 不落盘」，这是本门的原始目的）。
                from xenon.engine.evidence_gate import has_successful_write

                if has_successful_write(tracker):
                    ledger.append(
                        LifecyclePhase.DELIVERY, EventKind.GATE_VERDICT, EvidenceSource.GATE,
                        {"phase": phase, "passed": False, "degraded": True,
                         "reason": verdict.reason},
                    )
                    continue
                raise RuntimeError(f"delivery evidence gate failed ({phase}): {verdict.reason}")
        pack = EvidencePack.build(ledger)
        ledger.append(
            LifecyclePhase.DELIVERY, EventKind.DELIVERY, EvidenceSource.ENGINE,
            {"event_count": pack.event_count, "gate_failures": pack.gate_failures},
        )
        return EvidencePack.build(ledger)

    def delivery_gate_verdict(
        self,
        *,
        context: AgentContext | None = None,
        output: str = "",
        tracker: Any = None,
        workspace_root: Any = None,
    ) -> Any:
        """只做交付闸门判定（不 raise），供引擎补救循环预检。

        与 ``finalize_evidence`` 共享同一 Gate 管线（fix + output 两个 phase），
        但失败时返回 verdict 而非抛异常——让引擎有机会注入补救提示、
        再迭代一轮、再验证。这是「贴 diff 不落盘」的根因修复：拦截
        不是终点，拦截结果要反馈回 LLM。
        """
        ctx = context or AgentContext()
        ledger = self.evidence_ledger or ctx.get("_evidence_ledger")
        if ledger is None:
            raise RuntimeError("evidence ledger is not bound to this task")
        for phase, kwargs in (
            ("fix", {"tracker": tracker}),
            ("output", {"output": output, "tracker": tracker, "workspace_root": workspace_root}),
        ):
            verdict = self.gate_failed(phase, ctx, **kwargs)
            if verdict is not None:
                return verdict
        return None

    def delivery_remediation_prompt(self, verdict: Any) -> str:
        """把交付闸门拦截转成补救指令（单一真相源，供各引擎注入）。

        FileClaimGate 拦截的典型场景：LLM 声称修改/创建了文件，但工具
        执行记录里没有对应 write/edit 证据（「贴 diff 不落盘」）。补救
        指令明确要求：实际调用写工具落盘，落盘后用只读工具验证，再交付。
        """
        reason = getattr(verdict, "reason", str(verdict))
        return (
            "⚠️ 交付校验未通过，请修正后重新交付：\n"
            f"- 校验原因：{reason}\n"
            "- 你声称修改/创建了文件，但工具执行记录中没有对应的写操作证据。\n"
            "- 请实际调用 write_file / edit_file / batch_write 等写工具将改动落盘，"
            "落盘后可用 read_file 验证内容，确认无误后再给出 final_answer。\n"
            "- 不要只输出 diff 文本或描述性文字代替真实修改。"
        )

    def set_execution_policy(self, policy: ExecutionPolicy) -> None:
        """Bind a policy to this engine (combined graphs use the graph binder)."""

        self.execution_policy = policy
        self.request_timeout = policy.request_timeout

    def _provider_request_options(self, phase: str) -> dict[str, Any]:
        """Return the sole provider retry/timeout settings for this request."""

        policy = self.execution_policy
        policy.wait_for_request_slot(phase)
        timeout = policy.request_budget(phase)
        return {
            "timeout": timeout,
            "max_retries": policy.provider_attempts,
        }

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

    def steer(self, text: str) -> bool:
        """任务运行中注入一条用户补充/修改要求。

        线程安全：REPL 的输入监听线程在引擎运行时调用。
        消息进入队列后由引擎在下一个迭代检查点消费（当前工具调用
        不被打断，避免副作用中途掐断）。返回是否入队成功。
        """
        if not text or not text.strip():
            return False
        self._steering_queue.put({
            "text": text.strip(),
            "ts": time.time(),
        })
        return True

    def _drain_steering(self) -> list[dict[str, Any]]:
        """取出并记录当前所有待消费的 steering 消息（FIFO）。

        引擎在每个迭代/步骤循环顶部调用；空队列返回 []。已消费消息
        记录到 ``steering_consumed`` 供 REPL 展示与测试断言。
        """
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
        """每轮 run() 开头清空队列与消费记录（steering 不跨 run 串扰）。"""
        while True:
            try:
                self._steering_queue.get_nowait()
            except queue.Empty:
                break
        self.steering_consumed = []

    @staticmethod
    def steering_prompt(msgs: list[dict[str, Any]]) -> str:
        """把一条或多条 steering 消息渲染成注入指令（单一真相源）。

        语义约定（由 LLM 自行判断，程序不做意图分类）：
        补充的信息并入原任务；修改/新增要求调整后续步骤；
        已完成的工作不重复。返回的文本由各引擎拼进下一轮 prompt。
        """
        joined = "\n".join(
            f"- {m.get('text', '')}" for m in msgs
        )
        return (
            "用户在任务执行过程中补充了要求（原任务继续有效）：\n"
            f"{joined}\n\n"
            "请判断这些补充是对原任务的补充、修改还是新增要求，"
            "并据此调整后续步骤。已完成且仍然有效的工作不要重复执行；"
            "若需要调整已完成的产物，请明确指出并重新执行相关步骤。"
        )

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

    def _context_messages(self, *, stable: bool | None = None) -> list[dict[str, str]]:
        """Return replaceable context layers, optionally by cache tier."""
        if self._ctx_mgr is None:
            return []
        return self._ctx_mgr.get_context_messages(stable=stable)

    def _cache_ordered_context(
        self,
        history: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Compose context without invalidating the reusable prompt prefix.

        Engine-specific fixed instructions are added by the caller.  Stable
        project context follows, then the already established conversation.
        Working memory and query-dependent retrieval are volatile and stay at
        the tail, immediately before the caller appends the current user turn.
        """
        result: list[dict[str, Any]] = []
        result.extend(self._context_messages(stable=True))
        result.extend(history)
        if (
            self._ctx_mgr is not None
            and self._ctx_mgr.current_request_context_frozen()
        ):
            return result
        memory_message = self._working_memory_message()
        if memory_message is not None:
            result.append(memory_message)
        result.extend(self._context_messages(stable=False))
        return result

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
        if turn <= 0:
            return messages
        # 压缩时机必须是 fail-safe：定期压缩抑制 O(n²) 增长，但一旦已经逼近
        # 上下文窗口就不能再等下一个周期——下一次 provider 调用就会超限失败。
        # 压缩本身会把体积降到阈值以下，所以这个分支不会每轮重复触发。
        urgent = self._near_context_window(messages, ratio=0.75)
        if not urgent and turn % every != 0:
            return messages
        # ContextManager 的摘要格式不能表达 provider-issued tool_call_id。
        # 原生工具协议消息改走 block 级压缩，保持 tool_calls/tool 成对完整；
        # 绝不把它们交给 ContextManager 摘要，那会产生无效历史。
        if any(message.get("role") == "tool" or message.get("tool_calls") for message in messages):
            return self._compact_native_tool_messages(messages)
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
    def _split_protocol_blocks(
        messages: list[dict[str, Any]],
    ) -> list[list[dict[str, Any]]]:
        """把消息切成「原子块」，使 tool 协议成对关系永不被拆开。

        OpenAI/DeepSeek 协议约束：带 ``tool_calls`` 的 assistant 消息之后，必须
        紧跟每个 ``tool_call_id`` 对应的 ``role="tool"`` 消息。裁剪历史时这组消息
        必须作为一个整体保留或整体丢弃——只丢一半会让请求直接被 provider 拒绝。

        返回块列表；每块要么是单条普通消息，要么是 ``[assistant(+tool_calls),
        tool, tool, ...]``。孤立的 ``role="tool"``（上游异常导致）并入前一块，
        避免它单独成块后被丢弃而留下悬挂引用。
        """
        blocks: list[list[dict[str, Any]]] = []
        for message in messages:
            role = message.get("role")
            if role == "tool":
                # tool 消息永远属于前一个 assistant 块；没有前块说明历史已损坏，
                # 单独成块以便后续整体丢弃。
                if blocks and blocks[-1] and blocks[-1][0].get("tool_calls"):
                    blocks[-1].append(message)
                else:
                    blocks.append([message])
                continue
            blocks.append([message])
        return blocks

    def _compact_native_tool_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        keep_recent_blocks: int = 6,
    ) -> list[dict[str, Any]]:
        """在保持 tool 协议完整的前提下压缩原生工具调用历史。

        根因背景：原生工具协议下 in-run 压缩被整体跳过，长任务的 messages 会一直
        增长到超出上下文窗口，下一次 provider 调用直接失败。修复策略不是「删消息」
        而是「按协议块裁剪」：

        1. 保留 system 消息与第一条 user 消息（任务定义，丢了模型会跑偏）；
        2. 保留最近 ``keep_recent_blocks`` 个块（近期推理链）；
        3. 中间被丢弃的块折叠成一条 user 摘要消息，说明丢了多少轮工具交互——
           普通 user 消息不需要 tool 配对，所以这样替换后历史依然合法。

        只有在确实超出预算时才裁剪；否则原样返回，避免无谓丢失上下文。
        """
        if not self._near_context_window(messages, ratio=0.6):
            return messages
        try:
            blocks = self._split_protocol_blocks(messages)
            head: list[list[dict[str, Any]]] = []
            rest = blocks
            # 头部：连续的 system 消息 + 紧随的第一条 user 消息
            while rest and rest[0][0].get("role") == "system":
                head.append(rest.pop(0))
            if rest and rest[0][0].get("role") == "user":
                head.append(rest.pop(0))
            if len(rest) <= keep_recent_blocks:
                return messages
            dropped = rest[:-keep_recent_blocks]
            tail = rest[-keep_recent_blocks:]
            dropped_tool_results = sum(
                1
                for block in dropped
                for message in block
                if message.get("role") == "tool"
            )
            summary = {
                "role": "user",
                "content": (
                    "[历史已压缩] 为控制上下文长度，已省略 "
                    f"{len(dropped)} 轮较早的推理与 {dropped_tool_results} 条工具结果。"
                    "上述任务目标与下面最近的执行记录仍然有效；"
                    "如需早期结果请重新调用对应工具确认，不要凭记忆假设。"
                ),
            }
            compacted = [
                message for block in head for message in block
            ]
            compacted.append(summary)
            compacted.extend(message for block in tail for message in block)
            logger.info(
                "原生工具协议 in-run 压缩：%s → %s 条消息（丢弃 %s 块）",
                len(messages),
                len(compacted),
                len(dropped),
            )
            return compacted
        except Exception as e:  # noqa: BLE001 — 压缩绝不能中断主循环
            logger.warning(
                f"原生工具协议压缩失败（已忽略，沿用原 messages）: {e}"
            )
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
        cache_phase: str | None = None,
    ) -> str:
        """Call one model chain with one explicitly owned retry loop."""

        policy = self.execution_policy
        for retry in range(policy.chain_retries + 1):
            policy.check("llm_chain")
            try:
                result = self._call_llm_once(
                    messages,
                    max_tokens,
                    model_priority=model_priority,
                    cache_phase=cache_phase,
                )
                return result
            except EngineDeadlineExceeded:
                raise
            except RuntimeError as exc:
                cause = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
                if not self._is_transient_error(cause) or retry >= policy.chain_retries:
                    raise
                wait = self._extract_retry_after(cause, default=2.0 * (retry + 1))
                logger.warning(
                    "全链瞬时失败(%s)，退避 %.1fs 后重试(%s/%s)",
                    type(cause).__name__, wait, retry + 1, policy.chain_retries,
                )
                policy.sleep(wait)
        raise AssertionError("unreachable retry loop")

    def _call_llm_once(
        self,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        *,
        model_priority: list[str] | None = None,
        cache_phase: str | None = None,
    ) -> str:
        """调用 LLM，支持多模型 fallback。

        ``max_tokens`` 优先级：显式入参 > ``ModelConfig.max_tokens`` > 8192 默认；
        ``chat_completion`` 再按厂商上限钳制（B4）。``api_key``/``base_url`` 按
        模型覆盖（B7）。温度取 ``self.temperature``。

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
        effective_cache_phase = cache_phase or self._active_cache_phase

        def tp(message: str) -> str:
            return f"{prefix(self.run_id, call_id)} {message}"
        last_error: Exception | None = None
        for model_id in (model_priority or self.model_priority):
            started_at = time.monotonic()
            request_started = False
            request_succeeded = False
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
                    "cache_context": self._cache_context(effective_cache_phase),
                    "cache_lane_registry": (
                        getattr(self._ctx_mgr, "prompt_lanes", None)
                        if self._ctx_mgr is not None else None
                    ),
                }
                request_options.update(
                    self._provider_request_options(f"llm:{model_id}")
                )
                effort = getattr(mc, "reasoning_effort", "") if mc else ""
                if effort:
                    request_options["reasoning_effort"] = effort
                self.execution_policy.emit(
                    "provider_request_start",
                    phase=f"llm:{model_id}",
                    timeout=request_options["timeout"],
                    provider_attempts=request_options["max_retries"],
                )
                request_started = True
                result = chat_completion(model_id, messages, **request_options)
                # v0.4.0: record success to model pool
                if self.model_pool:
                    self.model_pool.record_success(
                        model_id,
                        time.monotonic() - started_at,
                    )
                self.last_model_used = model_id
                request_succeeded = True
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
            except EngineDeadlineExceeded:
                raise
            except Exception as e:
                if self.model_pool:
                    self._record_model_failure(model_id)
                last_error = e
                logger.warning(tp(f"模型 {model_id} 失败: {e}，尝试下一个..."))
            finally:
                if request_started:
                    self.execution_policy.emit(
                        "provider_request_end",
                        phase=f"llm:{model_id}",
                        success=request_succeeded,
                    )
                # P2: 释放并发计数(无论成败)
                if self.model_pool:
                    self.model_pool.release(model_id)
        self.callback.on_error(f"所有模型均调用失败: {last_error}")
        raise RuntimeError(f"所有模型均调用失败: {last_error}") from last_error

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
        cache_phase: str | None = None,
    ) -> Any:
        """单层 native FC 调用，遍历 ``model_priority``。

        返回 ``LLMResponse`` 或 ``None``（本层降级信号）。

        错误分流（与 ``_call_llm`` 一致 + 降级语义）：
        - 401/403 = 终端错误，立即上抛（认证坏 Key 切模型无意义）；
        - 400 = 该模型可能**不支持 tools/response_format** → 试下一个模型，
          全部 400 则本层降级（返回 None），让外层切到下一层 tier；
        - 429/5xx/网络/截断 = 瞬时 → 试下一个模型，全败立即报错，不能把
          供应商故障误判为协议不兼容并重复请求其他 tier。
        """
        last_error: Exception | None = None
        compatibility_only = True
        for model_id in self.model_priority:
            self.execution_policy.check(f"native:{model_id}")
            request_started = False
            request_succeeded = False
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
                    "cache_context": self._cache_context(
                        cache_phase or self._active_cache_phase
                    ),
                    "cache_lane_registry": (
                        getattr(self._ctx_mgr, "prompt_lanes", None)
                        if self._ctx_mgr is not None else None
                    ),
                }
                request_options.update(
                    self._provider_request_options(f"native:{model_id}")
                )
                effort = getattr(mc, "reasoning_effort", "") if mc else ""
                if effort:
                    request_options["reasoning_effort"] = effort
                self.execution_policy.emit(
                    "provider_request_start",
                    phase=f"native:{model_id}",
                    timeout=request_options["timeout"],
                    provider_attempts=request_options["max_retries"],
                )
                request_started = True
                response = chat_completion_with_tools(
                    model_id, messages, **request_options,
                )
                self.last_model_used = model_id
                request_succeeded = True
                return response
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (401, 403):
                    self.callback.on_error(
                        f"模型 {model_id} 认证失败 ({status})，请检查 API Key")
                    raise RuntimeError(
                        f"模型 {model_id} 认证失败 ({status})，请检查 API Key") from e
                # 400（不支持 tools/format）/ 429 / 5xx：试下一个模型
                if status != 400:
                    compatibility_only = False
                last_error = e
                logger.warning(
                    f"模型 {model_id} native 调用 HTTP {status}: {e}，尝试下一个...")
            except (
                httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
                httpx.RemoteProtocolError, httpx.WriteError, httpx.PoolTimeout,
            ) as e:
                compatibility_only = False
                last_error = e
                logger.warning(f"模型 {model_id} 网络错误 ({type(e).__name__}): {e}，尝试下一个...")
            except EngineDeadlineExceeded:
                raise
            except ResponseTruncatedError as e:
                compatibility_only = False
                last_error = e
                logger.warning(
                    f"模型 {model_id} native 响应截断，尝试下一个..."
                )
            except Exception as e:  # noqa: BLE001 — 本层降级，不中断
                compatibility_only = False
                last_error = e
                logger.warning(f"模型 {model_id} native 调用失败: {e}，尝试下一个...")
            finally:
                if request_started:
                    self.execution_policy.emit(
                        "provider_request_end",
                        phase=f"native:{model_id}",
                        success=request_succeeded,
                    )
        if isinstance(last_error, ResponseTruncatedError):
            raise last_error
        if last_error is not None and not compatibility_only:
            raise RuntimeError(
                f"native provider request failed: {last_error}"
            ) from last_error
        logger.warning(f"_call_with_tools_once 本层不兼容 ({last_error})，降级")
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
        "code_index", "ast_analyze", "web_fetch", "docs_fetch",
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

        # Never run side-effecting actions concurrently.  The model may return
        # a JSON array containing writes/commands even though the prompt asks
        # it not to; prompt text is not a safety boundary.  Fall back to the
        # same ordered path used for a single action.
        parallel_allowed = all(
            action.get("action", "") in self._PARALLEL_SAFE_TOOLS
            for action in actions
        )
        if len(actions) <= 1 or not parallel_allowed:
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
        *,
        cache_phase: str | None = None,
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
        if self._native_request_failed:
            logger.info(
                "native 请求此前整体失败，本次直接走文本协议（熔断）"
            )
            if max_tokens is None:
                return self._call_llm(messages)
            return self._call_llm(messages, max_tokens=max_tokens)
        if not tools_schema and not response_format:
            if max_tokens is None:
                return self._call_llm(messages)
            return self._call_llm(messages, max_tokens=max_tokens)

        tiers = [
            ("tier1_tools+format", tools_schema, response_format),
            ("tier2_tools_only", tools_schema, None),
            ("tier3_format_only", None, response_format),
        ]
        # Filter the empty fallback and duplicate shapes (tools-only input used
        # to produce identical tier1/tier2 requests).
        unique_tiers = []
        seen_shapes: set[tuple[bool, bool]] = set()
        for name, tools, fmt in tiers:
            shape = (bool(tools), bool(fmt))
            if not any(shape) or shape in seen_shapes:
                continue
            seen_shapes.add(shape)
            unique_tiers.append((name, tools, fmt))
        tiers = unique_tiers

        for tier_name, tools, fmt in tiers:
            capability_key = (
                tuple(self.model_priority), bool(tools), bool(fmt)
            )
            if capability_key in self._unsupported_native_shapes:
                logger.debug(
                    "_call_llm_native 跳过已确认不兼容层: %s", tier_name
                )
                continue
            self.execution_policy.check(tier_name)
            try:
                resp = self._call_with_tools_once(
                    messages,
                    tools,
                    fmt,
                    max_tokens,
                )
            except RuntimeError as exc:
                # v0.8.3: native 全模型失败（5xx/网络断连）→ 熔断并整体回退
                # 文本协议，而不是让引擎整个 run 崩溃（SWE-bench react 4 例）。
                if "native provider request failed" in str(exc):
                    logger.warning(
                        "native 请求整体失败（%s），熔断并回退文本协议", exc,
                    )
                    self._native_request_failed = True
                    break
                raise
            except ResponseTruncatedError as exc:
                # A native tool envelope is atomic and cannot be resumed by
                # appending a user message. Try the next compatibility tier;
                # the final plain-text fallback has protocol-aware repair.
                logger.warning(
                    "_call_llm_native %s 响应截断，降级下一层: %s",
                    tier_name,
                    exc,
                )
                continue
            if resp is None:
                self._unsupported_native_shapes.add(capability_key)
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
        if max_tokens is None:
            return self._call_llm(messages)
        return self._call_llm(messages, max_tokens=max_tokens)

    def _cache_context(self, phase: str) -> dict[str, Any]:
        """Return stable request attribution shared by all engine call paths."""
        epoch = getattr(self._ctx_mgr, "cache_epoch", 0) if self._ctx_mgr else 0
        return {
            "engine": type(self).__name__.removesuffix("Engine").lower(),
            "phase": str(phase or "request").lower(),
            "context_epoch": epoch,
            "event_cursor": (
                getattr(self._ctx_mgr, "event_cursor", 0)
                if self._ctx_mgr is not None else 0
            ),
        }

    def _call_llm_for_phase(
        self,
        phase: str,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        *,
        model_priority: list[str] | None = None,
    ) -> str:
        """Tag a call phase without changing the replaceable ``_call_llm`` API."""
        previous = self._active_cache_phase
        self._active_cache_phase = phase
        try:
            if model_priority is None and max_tokens is None:
                return self._call_llm(messages)
            if model_priority is None:
                return self._call_llm(messages, max_tokens=max_tokens)
            if max_tokens is None:
                return self._call_llm(
                    messages,
                    model_priority=model_priority,
                )
            return self._call_llm(
                messages,
                max_tokens=max_tokens,
                model_priority=model_priority,
            )
        finally:
            self._active_cache_phase = previous

    def _call_llm_native_for_phase(
        self,
        phase: str,
        messages: list[dict[str, str]],
        tools_schema: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Tag a native-tool phase while preserving its public test seam."""
        previous = self._active_cache_phase
        self._active_cache_phase = phase
        try:
            if max_tokens is None:
                return self._call_llm_native(
                    messages,
                    tools_schema,
                    response_format,
                )
            return self._call_llm_native(
                messages,
                tools_schema,
                response_format,
                max_tokens,
            )
        finally:
            self._active_cache_phase = previous

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
                answer = self._call_llm_for_phase("synthesis", [
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
