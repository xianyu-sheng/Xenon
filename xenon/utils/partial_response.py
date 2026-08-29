"""
部分响应处理 - 智能检查点机制的核心数据结构。

当模型因网络问题中断时，保存已生成的部分内容，用于后续续写。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PartialContent:
    """部分生成内容的数据结构。

    记录模型中断前已生成的内容及相关元信息，用于智能续写。
    """

    content: str
    """已生成的文本内容"""

    tokens_generated: int
    """已生成的 token 数量（估算值）"""

    model_id: str
    """生成该内容的模型 ID"""

    timestamp: float = field(default_factory=time.time)
    """生成时间戳"""

    finish_reason: str | None = None
    """中断原因（如 'network_error', 'timeout' 等）"""

    usage: dict[str, Any] | None = None
    """已消耗的 token 用量（如果可获取）"""

    metadata: dict[str, Any] = field(default_factory=dict)
    """额外的元数据（如检查点信息、边界信息等）"""

    def __len__(self) -> int:
        """返回已生成内容的字符长度"""
        return len(self.content)

    def is_valid(self, min_length: int = 100) -> bool:
        """判断部分内容是否有效（长度是否达到续写阈值）。

        Args:
            min_length: 最小有效长度（字符数）

        Returns:
            True 如果内容长度 >= min_length
        """
        return len(self.content) >= min_length

    def estimate_tokens(self) -> int:
        """估算内容的 token 数量（粗略估计：英文 4 字符/token，中文 2 字符/token）。

        Returns:
            估算的 token 数量
        """
        if self.tokens_generated > 0:
            return self.tokens_generated

        # 简单估算：统计中文字符和英文字符
        chinese_chars = sum(1 for c in self.content if '一' <= c <= '鿿')
        english_chars = len(self.content) - chinese_chars

        # 中文约 2 字符/token，英文约 4 字符/token
        estimated = chinese_chars // 2 + english_chars // 4
        return max(estimated, 1)


class PartialResponseError(Exception):
    """携带部分生成内容的异常。

    当 LLM 调用因网络问题中断时抛出此异常，携带已生成的部分内容，
    以便引擎层实现智能续写。
    """

    def __init__(self, partial: PartialContent, original_error: Exception | None = None):
        """初始化部分响应异常。

        Args:
            partial: 已生成的部分内容
            original_error: 导致中断的原始异常
        """
        self.partial = partial
        self.original_error = original_error

        # 构造友好的错误消息
        msg_parts = [
            f"模型 {partial.model_id} 生成中断",
            f"已生成 {len(partial.content)} 字符",
        ]
        if partial.tokens_generated > 0:
            msg_parts.append(f"(约 {partial.tokens_generated} tokens)")
        if partial.finish_reason:
            msg_parts.append(f"原因: {partial.finish_reason}")

        message = ", ".join(msg_parts)
        super().__init__(message)

    def can_continue(self, min_length: int = 100) -> bool:
        """判断是否可以续写（部分内容是否有效）。

        Args:
            min_length: 最小有效长度

        Returns:
            True 如果可以续写
        """
        return self.partial.is_valid(min_length)

    def __repr__(self) -> str:
        return (
            f"PartialResponseError("
            f"model={self.partial.model_id}, "
            f"length={len(self.partial.content)}, "
            f"reason={self.partial.finish_reason})"
        )


@dataclass
class ContinuationContext:
    """续写上下文信息。

    记录续写操作的元信息，用于统计、日志和调试。
    """

    original_model: str
    """原始模型 ID"""

    continuation_model: str
    """续写模型 ID"""

    partial_length: int
    """部分内容长度（字符数）"""

    partial_tokens: int
    """部分内容 token 数"""

    continuation_prompt: str
    """续写提示语"""

    started_at: float = field(default_factory=time.time)
    """续写开始时间"""

    completed_at: float | None = None
    """续写完成时间"""

    success: bool = False
    """是否成功续写"""

    error: str | None = None
    """续写失败的错误信息"""

    tokens_saved: int = 0
    """节省的 token 数量（估算）"""

    def mark_completed(self, success: bool = True, error: str | None = None) -> None:
        """标记续写完成。

        Args:
            success: 是否成功
            error: 失败时的错误信息
        """
        self.completed_at = time.time()
        self.success = success
        self.error = error

    def duration(self) -> float:
        """计算续写耗时（秒）。

        Returns:
            耗时秒数，如果未完成则返回当前经过的时间
        """
        end = self.completed_at or time.time()
        return end - self.started_at

    def to_dict(self) -> dict[str, Any]:
        """转换为字典（用于日志和统计）。

        Returns:
            包含所有字段的字典
        """
        return {
            "original_model": self.original_model,
            "continuation_model": self.continuation_model,
            "partial_length": self.partial_length,
            "partial_tokens": self.partial_tokens,
            "continuation_prompt": self.continuation_prompt[:50] + "...",
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "success": self.success,
            "error": self.error,
            "tokens_saved": self.tokens_saved,
            "duration": self.duration(),
        }
