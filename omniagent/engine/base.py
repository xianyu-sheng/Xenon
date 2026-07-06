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
