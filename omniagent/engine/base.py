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

import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx

from omniagent.engine.callbacks import EngineCallback
from omniagent.engine.context import AgentContext
from omniagent.utils.llm_client import ResponseTruncatedError, chat_completion

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
    ) -> None:
        self.model_priority = list(model_priority)
        self.callback = callback or EngineCallback()
        # alias -> ModelConfig，供 _call_llm 读每模型 max_tokens/api_key/base_url（B4/B7）
        self.model_configs = model_configs or {}
        self.temperature = temperature
        # F6: 协作式中断标志，外部调 interrupt() 后 run() 在下一轮退出
        self._interrupted: bool = False
        # F4: 本次 run 注入的 ContextManager（run 起点设置，供 _history_messages 消费）
        self._ctx_mgr: Any = None

    def interrupt(self) -> None:
        """F6: 协作式中断——外部调用后，run() 在下一轮迭代顶部退出。"""
        self._interrupted = True

    def _reset_interrupt(self) -> None:
        """每轮 run() 开头重置中断标志。"""
        self._interrupted = False

    def _context_window(self) -> int:
        """当前激活模型的上下文窗口（取最小=瓶颈模型）；未知则 128000。"""
        windows = [
            getattr(mc, "context_window", 0)
            for mc in self.model_configs.values()
            if getattr(mc, "context_window", 0) > 0
        ]
        return min(windows) if windows else 128000

    def _near_context_window(self, messages: list[dict[str, str]], ratio: float = 0.8) -> bool:
        """F6: 估算 messages token 是否接近上下文窗口（默认 80%）。

        粗估（字符数//2）仅用于预算预警/拒绝大 observation，非精确计费。
        """
        window = self._context_window()
        if window <= 0:
            return False
        est = sum(len(m.get("content", "")) for m in messages) // 2
        return est > ratio * window

    def _history_messages(self, context: Any) -> list[dict[str, str]]:
        """F4: 优先消费注入的 ctx_mgr（已压缩）消息，否则回退 AgentContext 历史。

        返回非 system 消息（system 由各引擎自行注入自己的 system_prompt）。
        """
        if self._ctx_mgr is not None:
            return [m for m in self._ctx_mgr.get_messages() if m.get("role") != "system"]
        if context:
            return context.get_conversation_messages()
        return []

    def _maybe_compact_messages(
        self,
        messages: list[dict[str, str]],
        turn: int,
        every: int = 5,
    ) -> list[dict[str, str]]:
        """F4: 每 ``every`` 轮压缩 in-run messages，复用 ContextManager 的 F3 压缩逻辑。

        引擎局部 ``messages`` 在迭代中 O(n) 增长，每轮重发给 LLM 造成 O(n²) token
        成本（§8.9.6）。每 5 轮用临时 ContextManager 跑一次 F3 compact（6 段/安全
        截断），把早期轨迹摘要化、保留近期上下文。无 model_priority 或 LLM 失败时
        自动回退正则摘要（F3 已实现）。
        """
        if turn <= 0 or turn % every != 0:
            return messages
        try:
            from omniagent.repl.context_manager import ContextManager

            tmp = ContextManager(max_tokens=self._context_window())
            for m in messages:
                tmp.add_message(m.get("role", "user"), m.get("content", ""))
            tmp.compact(model_priority=self.model_priority or None)
            compacted = tmp.get_messages()
            return compacted if compacted else messages
        except Exception as e:  # noqa: BLE001 — 压缩绝不能中断主循环
            logger.warning(f"in-run 压缩失败（已忽略，沿用原 messages）: {e}")
            return messages

    def _call_llm(self, messages: list[dict[str, str]], max_tokens: int | None = None) -> str:
        """调用 LLM，支持多模型 fallback。

        ``max_tokens`` 优先级：显式入参 > ``ModelConfig.max_tokens`` > 8192 默认；
        ``chat_completion`` 再按厂商上限钳制（B4）。``api_key``/``base_url`` 按
        模型覆盖（B7）。温度取 ``self.temperature``（novel=0.8，其余=0.3）。

        错误分流（R1 / Q9）：
        - 401/403（认证失败）、400（请求被拒）= **终端错误**，切模型无意义，
          立即上抛并 ``callback.on_error``，避免用坏 Key 逐一慢试全部模型；
        - 429/5xx/网络错误/响应截断 = **瞬时错误**，切下一个模型；
        - 全部模型失败 → ``callback.on_error`` + 抛 RuntimeError。
        """
        last_error: Exception | None = None
        for model_id in self.model_priority:
            try:
                mc = self.model_configs.get(model_id)
                mt = max_tokens or getattr(mc, "max_tokens", None) or 8192
                creds = None
                base = None
                if mc:
                    base = getattr(mc, "base_url", "") or None
                    mk = getattr(mc, "api_key", "") or ""
                    if mk and "/" in model_id:
                        creds = {model_id.split("/", 1)[0].lower(): mk}
                return chat_completion(
                    model_id, messages, max_tokens=mt,
                    temperature=self.temperature, credentials=creds, base_url=base,
                )
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
                last_error = e
                logger.warning(f"模型 {model_id} HTTP {status} 失败: {e}，尝试下一个...")
            except (
                httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
                httpx.RemoteProtocolError, httpx.WriteError, httpx.PoolTimeout,
            ) as e:
                last_error = e
                logger.warning(f"模型 {model_id} 网络错误 ({type(e).__name__}): {e}，尝试下一个...")
            except ResponseTruncatedError as e:
                last_error = e
                logger.warning(f"模型 {model_id} 响应截断: {e}，尝试下一个...")
            except Exception as e:
                last_error = e
                logger.warning(f"模型 {model_id} 失败: {e}，尝试下一个...")
        self.callback.on_error(f"所有模型均调用失败: {last_error}")
        raise RuntimeError(f"所有模型均调用失败: {last_error}")

    @abstractmethod
    def run(self, user_input: str, context: AgentContext | None = None) -> str:
        """子类实现主循环。"""
        raise NotImplementedError
