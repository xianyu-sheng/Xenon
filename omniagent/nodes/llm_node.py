"""
LLMNode — 大语言模型调用节点（支持多模型优先级轮询 / Fallback）。

核心逻辑：
1. 接收一个 model_priority 列表（如 ["anthropic/claude-3-5-sonnet", "openai/gpt-4o"]）。
2. 按顺序尝试调用，若遇到限流 (429) 或服务端错误 (5xx) 自动切换到下一个模型。
3. 全部失败则抛出异常。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from omniagent.engine.context import AgentContext
from omniagent.nodes.base import BaseNode
from omniagent.utils.llm_client import chat_completion

logger = logging.getLogger(__name__)


class LLMNode(BaseNode):
    """大语言模型调用节点，支持多模型优先级 Fallback。"""

    def __init__(
        self,
        node_id: str,
        *,
        model_priority: list[str],
        prompt: str,
        output_slot: str | None = None,
        system_prompt: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        default_next: str | None = None,
    ) -> None:
        super().__init__(node_id, output_slot=output_slot, default_next=default_next)
        if not model_priority:
            raise ValueError("model_priority 不能为空")
        self.model_priority = model_priority
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.temperature = temperature

    def execute(self, context: AgentContext) -> dict[str, Any]:
        """
        执行 LLM 调用，支持多模型 Fallback。

        返回: {"model_used": str, "content": str}
        """
        messages = self._build_messages(context)
        last_error: Exception | None = None

        for model_id in self.model_priority:
            try:
                logger.info(f"[{self.id}] 尝试调用模型: {model_id}")
                content = chat_completion(
                    model_id=model_id,
                    messages=messages,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                )
                result = {"model_used": model_id, "content": content}
                self._write_output(context, content)
                logger.info(f"[{self.id}] 模型 {model_id} 调用成功")
                return result

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                last_error = e
                if status in (429, 500, 502, 503, 504):
                    logger.warning(
                        f"[{self.id}] 模型 {model_id} 返回 HTTP {status}，"
                        f"自动切换到下一个模型"
                    )
                    continue
                else:
                    raise  # 4xx 客户端错误（如 401 认证失败）不重试

            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                last_error = e
                logger.warning(f"[{self.id}] 模型 {model_id} 网络异常: {e}，切换下一个")
                continue

        raise RuntimeError(
            f"[{self.id}] 所有模型均调用失败: {self.model_priority}。"
            f"最后一个错误: {last_error}"
        )

    def _build_messages(self, context: AgentContext) -> list[dict[str, str]]:
        """构建消息列表，支持 prompt 中的 {variable} 上下文变量替换。"""
        resolved_prompt = self._resolve_template(self.prompt, context)
        messages: list[dict[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": resolved_prompt})
        return messages

    @staticmethod
    def _resolve_template(template: str, context: AgentContext) -> str:
        """将 prompt 中的 {key} 替换为 context 中对应值。"""
        import re
        def _replace(m: re.Match) -> str:
            key = m.group(1)
            val = context._store.get(key)
            return str(val) if val is not None else m.group(0)
        return re.sub(r"\{(\w+)\}", _replace, template)
