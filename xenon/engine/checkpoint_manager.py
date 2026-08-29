"""
检查点管理器 - Phase 3 核心模块。

在流式生成过程中周期性保存检查点，网络中断时从最近的检查点恢复。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Checkpoint:
    """检查点快照数据结构"""

    content: str
    """到该点的全部内容"""

    tokens: int
    """已生成的 token 数"""

    timestamp: float
    """保存时间戳"""

    sequence: int
    """检查点序号（从 0 开始）"""

    boundary_info: dict = field(default_factory=dict)
    """语义边界信息（可选）"""

    metadata: dict = field(default_factory=dict)
    """额外的元数据"""

    def __len__(self) -> int:
        """返回内容长度"""
        return len(self.content)

    def age(self) -> float:
        """返回检查点年龄（秒）"""
        return time.time() - self.timestamp

    def to_dict(self) -> dict:
        """转换为字典（用于日志和调试）"""
        return {
            "content_length": len(self.content),
            "tokens": self.tokens,
            "timestamp": self.timestamp,
            "sequence": self.sequence,
            "age": self.age(),
            "boundary_info": self.boundary_info,
            "metadata": self.metadata,
        }


class CheckpointManager:
    """
    检查点管理器。

    在流式生成过程中周期性保存检查点，当网络中断时能从最近的检查点恢复。

    示例:
        >>> manager = CheckpointManager(interval_tokens=500, max_checkpoints=5)
        >>> # 在流式生成中
        >>> accumulated_tokens = 0
        >>> for chunk in stream:
        ...     accumulated_tokens += estimate_tokens(chunk)
        ...     if manager.should_save(accumulated_tokens):
        ...         manager.save_checkpoint(accumulated_content, accumulated_tokens)
        >>> # 网络中断时
        >>> last_checkpoint = manager.get_last_checkpoint()
        >>> if last_checkpoint:
        ...     # 从检查点恢复
        ...     resume_from = last_checkpoint.content
    """

    def __init__(
        self,
        interval_tokens: int = 500,
        max_checkpoints: int = 5,
        enabled: bool = True,
    ):
        """
        初始化检查点管理器。

        Args:
            interval_tokens: 检查点间隔（tokens 数）
            max_checkpoints: 最多保留的检查点数量
            enabled: 是否启用检查点（可用于调试时禁用）
        """
        self.interval = interval_tokens
        self.max_checkpoints = max_checkpoints
        self.enabled = enabled

        self.checkpoints: list[Checkpoint] = []
        self.next_sequence = 0

    def should_save(self, accumulated_tokens: int) -> bool:
        """
        判断是否应该保存检查点。

        Args:
            accumulated_tokens: 已累积的 token 数

        Returns:
            True 如果应该保存检查点
        """
        if not self.enabled:
            return False

        # 第一个检查点：达到间隔即保存
        if not self.checkpoints:
            return accumulated_tokens >= self.interval

        # 后续检查点：距离上次间隔足够时保存
        last_checkpoint = self.checkpoints[-1]
        tokens_since_last = accumulated_tokens - last_checkpoint.tokens
        return tokens_since_last >= self.interval

    def save_checkpoint(
        self,
        content: str,
        tokens: int,
        boundary_info: dict | None = None,
        metadata: dict | None = None,
    ) -> Checkpoint:
        """
        保存检查点。

        Args:
            content: 到该点的全部内容
            tokens: 已生成的 token 数
            boundary_info: 语义边界信息（可选）
            metadata: 额外的元数据（可选）

        Returns:
            保存的检查点对象
        """
        if not self.enabled:
            # 禁用时创建但不保存
            return Checkpoint(
                content=content,
                tokens=tokens,
                timestamp=time.time(),
                sequence=-1,
                boundary_info=boundary_info or {},
                metadata=metadata or {},
            )

        checkpoint = Checkpoint(
            content=content,
            tokens=tokens,
            timestamp=time.time(),
            sequence=self.next_sequence,
            boundary_info=boundary_info or {},
            metadata=metadata or {},
        )

        self.checkpoints.append(checkpoint)
        self.next_sequence += 1

        # 淘汰旧检查点（保留最近的 N 个）
        if len(self.checkpoints) > self.max_checkpoints:
            removed = self.checkpoints.pop(0)
            # 可选：记录淘汰事件
            checkpoint.metadata["removed_checkpoint"] = removed.sequence

        return checkpoint

    def get_last_checkpoint(self) -> Checkpoint | None:
        """
        获取最近的检查点。

        Returns:
            最近的检查点，如果没有检查点返回 None
        """
        return self.checkpoints[-1] if self.checkpoints else None

    def get_checkpoint_at_tokens(self, target_tokens: int) -> Checkpoint | None:
        """
        获取指定 token 数之前的最近检查点。

        Args:
            target_tokens: 目标 token 数

        Returns:
            最近的检查点（tokens <= target_tokens），如果没有返回 None
        """
        for checkpoint in reversed(self.checkpoints):
            if checkpoint.tokens <= target_tokens:
                return checkpoint
        return None

    def get_checkpoint_by_sequence(self, sequence: int) -> Checkpoint | None:
        """
        根据序号获取检查点。

        Args:
            sequence: 检查点序号

        Returns:
            对应的检查点，如果不存在返回 None
        """
        for checkpoint in self.checkpoints:
            if checkpoint.sequence == sequence:
                return checkpoint
        return None

    def clear(self) -> int:
        """
        清除所有检查点。

        Returns:
            清除的检查点数量
        """
        count = len(self.checkpoints)
        self.checkpoints.clear()
        self.next_sequence = 0
        return count

    def stats(self) -> dict:
        """
        获取统计信息。

        Returns:
            包含统计信息的字典
        """
        if not self.checkpoints:
            return {
                "enabled": self.enabled,
                "total_checkpoints": 0,
                "interval_tokens": self.interval,
                "max_checkpoints": self.max_checkpoints,
                "checkpoints": [],
            }

        return {
            "enabled": self.enabled,
            "total_checkpoints": len(self.checkpoints),
            "interval_tokens": self.interval,
            "max_checkpoints": self.max_checkpoints,
            "oldest_sequence": self.checkpoints[0].sequence,
            "newest_sequence": self.checkpoints[-1].sequence,
            "total_tokens": self.checkpoints[-1].tokens,
            "checkpoints": [cp.to_dict() for cp in self.checkpoints],
        }

    def __len__(self) -> int:
        """返回当前保存的检查点数量"""
        return len(self.checkpoints)

    def __bool__(self) -> bool:
        """检查是否有检查点"""
        return len(self.checkpoints) > 0

    def __repr__(self) -> str:
        return (
            f"CheckpointManager("
            f"checkpoints={len(self.checkpoints)}, "
            f"interval={self.interval}, "
            f"max={self.max_checkpoints}, "
            f"enabled={self.enabled})"
        )
