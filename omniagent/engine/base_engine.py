"""
Base Engine — 所有引擎的抽象基类。

提供共享的:
- __init__: model_priority, callback 初始化
- _call_llm: 多模型回退 LLM 调用
- _inject_history: 对话历史注入
- run: 抽象方法（子类必须实现）
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from omniagent.engine.callbacks import EngineCallback
from omniagent.engine.context import AgentContext
from omniagent.utils.llm_client import chat_completion

logger = logging.getLogger(__name__)


class BaseEngine(ABC):
    """所有同步引擎的抽象基类。

    子类只需实现 run() 方法。_call_llm 和 _inject_history 由基类提供，
    子类可通过参数覆盖默认行为（temperature, max_non_system 等）。
    """

    def __init__(
        self,
        model_priority: list[str],
        *,
        callback: EngineCallback | None = None,
        **kwargs: Any,
    ) -> None:
        """初始化引擎。

        Args:
            model_priority: 模型 ID 优先级列表
            callback: 引擎回调（默认 EngineCallback 空实现）
            **kwargs: 子类特定属性，自动设置为实例属性
        """
        self.model_priority = model_priority
        self.callback = callback or EngineCallback()
        for k, v in kwargs.items():
            setattr(self, k, v)

    @abstractmethod
    def run(self, user_input: str, context: AgentContext | None = None) -> str:
        """执行引擎主循环。

        Args:
            user_input: 用户输入文本
            context: 可选的 AgentContext（含对话历史等共享状态）

        Returns:
            引擎执行的最终输出文本
        """
        ...

    def _call_llm(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 131072,
        temperature: float = 0.3,
        *,
        model_priority: list[str] | None = None,
    ) -> str:
        """调用 LLM，支持多模型 fallback。

        Args:
            messages: LLM 消息列表
            max_tokens: 最大输出 token 数
            temperature: 采样温度（创意任务可调高至 0.8）
            model_priority: 覆盖默认模型列表（用于按阶段分派不同模型角色）

        Returns:
            LLM 响应文本

        Raises:
            RuntimeError: 所有模型均调用失败
        """
        models = model_priority or self.model_priority
        last_error = None
        for model_id in models:
            try:
                return chat_completion(
                    model_id, messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except Exception as e:
                last_error = e
                logger.warning(f"模型 {model_id} 失败: {e}，尝试下一个...")
        raise RuntimeError(f"所有模型均调用失败: {last_error}")

    def _inject_history(
        self,
        messages: list[dict[str, str]],
        context: AgentContext | None,
        max_non_system: int = 10,
    ) -> None:
        """将对话历史注入到消息列表中（原地修改）。

        策略：
        - 取最近 N 条非 system 消息
        - 取最近 2 条 system 消息（保留 system_hint 等）
        - system 消息在前，非 system 在后

        Args:
            messages: 当前消息列表（会被原地修改）
            context: AgentContext（含对话历史），可为 None
            max_non_system: 最多保留的非 system 消息数（默认 10）
        """
        if context is None:
            return
        history = context.get_conversation_messages()
        if history:
            non_system = [m for m in history if m.get("role") != "system"][-max_non_system:]
            system_msgs = [m for m in history if m.get("role") == "system"][-2:]
            recent = system_msgs + non_system
            messages.extend(recent)
            logger.debug(
                "注入 %d 条对话历史 (含 %d 条 system)",
                len(recent), len(system_msgs),
            )
        else:
            logger.warning("无对话历史可注入！")


class AsyncBaseEngine(ABC):
    """所有异步引擎的抽象基类。

    与 BaseEngine 相同接口，但核心方法是 async。
    """

    def __init__(
        self,
        model_priority: list[str],
        *,
        callback: EngineCallback | None = None,
        **kwargs: Any,
    ) -> None:
        self.model_priority = model_priority
        self.callback = callback or EngineCallback()
        for k, v in kwargs.items():
            setattr(self, k, v)

    @abstractmethod
    async def run(self, user_input: str, context: AgentContext | None = None) -> str:
        """异步执行引擎主循环。"""
        ...

    async def _call_llm_async(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 131072,
        temperature: float = 0.3,
        *,
        model_priority: list[str] | None = None,
    ) -> str:
        """异步调用 LLM，支持多模型 fallback。"""
        from omniagent.utils.llm_client import chat_completion_async

        models = model_priority or self.model_priority
        last_error = None
        for model_id in models:
            try:
                return await chat_completion_async(
                    model_id, messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except Exception as e:
                last_error = e
                logger.warning(f"模型 {model_id} 失败: {e}，尝试下一个...")
        raise RuntimeError(f"所有模型均调用失败: {last_error}")

    def _inject_history(
        self,
        messages: list[dict[str, str]],
        context: AgentContext | None,
        max_non_system: int = 10,
    ) -> None:
        """将对话历史注入到消息列表中（与 BaseEngine 实现相同）。"""
        if context is None:
            return
        history = context.get_conversation_messages()
        if history:
            non_system = [m for m in history if m.get("role") != "system"][-max_non_system:]
            system_msgs = [m for m in history if m.get("role") == "system"][-2:]
            recent = system_msgs + non_system
            messages.extend(recent)
            logger.debug(
                "注入 %d 条对话历史 (含 %d 条 system)",
                len(recent), len(system_msgs),
            )
        else:
            logger.warning("无对话历史可注入！")
