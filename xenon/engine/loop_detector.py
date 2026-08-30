"""
循环检测器 - 用于检测 ReAct 引擎的原地打转。

通过分析连续轮次的输出相似度，判断是否陷入循环。
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass


@dataclass
class LoopDetectionResult:
    """循环检测结果"""

    is_loop: bool
    """是否检测到循环"""

    confidence: float
    """置信度 (0-1)"""

    loop_length: int
    """循环周期长度（如果检测到）"""

    similar_turns: list[int]
    """相似的轮次索引"""

    reason: str
    """检测原因"""


class LoopDetector:
    """
    循环检测器。

    通过多种启发式方法检测 ReAct 引擎是否陷入循环：
    1. 输出哈希值相似度
    2. 工具调用模式重复
    3. 错误消息重复
    4. 思考内容相似度

    示例:
        >>> detector = LoopDetector(window_size=5, similarity_threshold=0.8)
        >>> for turn in turns:
        ...     detector.add_turn(turn)
        ...     result = detector.check()
        ...     if result.is_loop:
        ...         print(f"检测到循环: {result.reason}")
        ...         break
    """

    def __init__(
        self,
        window_size: int = 5,
        similarity_threshold: float = 0.8,
        enabled: bool = True,
    ):
        """
        初始化循环检测器。

        Args:
            window_size: 检测窗口大小（比较最近 N 轮）
            similarity_threshold: 相似度阈值（超过则认为循环）
            enabled: 是否启用（可用于调试时禁用）
        """
        self.window_size = window_size
        self.similarity_threshold = similarity_threshold
        self.enabled = enabled

        # 历史记录
        self.turn_hashes: deque[str] = deque(maxlen=window_size * 2)
        self.tool_patterns: deque[str] = deque(maxlen=window_size * 2)
        self.error_messages: deque[str] = deque(maxlen=window_size * 2)
        self.thought_hashes: deque[str] = deque(maxlen=window_size * 2)

        self.turn_count = 0

    def add_turn(
        self,
        output: str,
        tool_calls: list[str] | None = None,
        error: str | None = None,
        thought: str | None = None,
    ) -> None:
        """
        添加一轮的信息。

        Args:
            output: 当前轮的输出文本
            tool_calls: 工具调用列表（如 ['read_file', 'write_file']）
            error: 错误消息（如果有）
            thought: 思考内容（reasoning）
        """
        if not self.enabled:
            return

        self.turn_count += 1

        # 1. 输出哈希
        output_hash = self._hash(output)
        self.turn_hashes.append(output_hash)

        # 2. 工具调用模式
        tool_pattern = ",".join(sorted(tool_calls or []))
        self.tool_patterns.append(tool_pattern)

        # 3. 错误消息
        if error:
            error_hash = self._hash(error)
            self.error_messages.append(error_hash)
        else:
            self.error_messages.append("")

        # 4. 思考哈希
        if thought:
            thought_hash = self._hash(thought)
            self.thought_hashes.append(thought_hash)
        else:
            self.thought_hashes.append("")

    def check(self) -> LoopDetectionResult:
        """
        检查是否存在循环。

        Returns:
            检测结果
        """
        if not self.enabled:
            return LoopDetectionResult(
                is_loop=False,
                confidence=0.0,
                loop_length=0,
                similar_turns=[],
                reason="检测器已禁用",
            )

        # 需要至少 3 轮才能检测
        if self.turn_count < 3:
            return LoopDetectionResult(
                is_loop=False,
                confidence=0.0,
                loop_length=0,
                similar_turns=[],
                reason="轮次不足，需要至少 3 轮",
            )

        # 方法 1: 检查输出哈希重复
        result = self._check_hash_similarity(self.turn_hashes, "输出")
        if result.is_loop:
            return result

        # 方法 2: 检查工具调用模式重复
        result = self._check_pattern_repeat(self.tool_patterns, "工具调用")
        if result.is_loop:
            return result

        # 方法 3: 检查错误消息重复
        result = self._check_pattern_repeat(
            self.error_messages, "错误消息", ignore_empty=True
        )
        if result.is_loop:
            return result

        # 方法 4: 检查思考内容相似
        result = self._check_hash_similarity(self.thought_hashes, "思考内容")
        if result.is_loop:
            return result

        return LoopDetectionResult(
            is_loop=False,
            confidence=0.0,
            loop_length=0,
            similar_turns=[],
            reason="未检测到循环",
        )

    def _check_hash_similarity(
        self, hashes: deque[str], name: str
    ) -> LoopDetectionResult:
        """检查哈希值相似度"""
        if len(hashes) < 3:
            return LoopDetectionResult(
                is_loop=False, confidence=0.0, loop_length=0, similar_turns=[], reason=""
            )

        recent_hashes = list(hashes)[-self.window_size :]

        # 检查最近的哈希是否有重复
        for i in range(len(recent_hashes) - 1, 0, -1):
            current = recent_hashes[i]
            if not current:  # 跳过空哈希
                continue

            # 查找之前是否有相同的哈希
            similar_indices = []
            for j in range(i):
                if recent_hashes[j] == current:
                    similar_indices.append(j)

            # 根据重复次数计算置信度
            # 3次或以上相同 → 高置信度 0.9（可能真的陷入循环）
            # 2次相同 → 中等置信度 0.7（可能只是巧合）
            if len(similar_indices) >= 2:
                confidence = 0.9 if len(similar_indices) >= 3 else 0.7
                return LoopDetectionResult(
                    is_loop=True,
                    confidence=confidence,
                    loop_length=i - similar_indices[-1],
                    similar_turns=similar_indices + [i],
                    reason=f"{name}哈希重复（轮次 {similar_indices + [i]}）",
                )

        return LoopDetectionResult(
            is_loop=False, confidence=0.0, loop_length=0, similar_turns=[], reason=""
        )

    def _check_pattern_repeat(
        self, patterns: deque[str], name: str, ignore_empty: bool = False
    ) -> LoopDetectionResult:
        """检查模式重复"""
        if len(patterns) < 3:
            return LoopDetectionResult(
                is_loop=False, confidence=0.0, loop_length=0, similar_turns=[], reason=""
            )

        recent_patterns = list(patterns)[-self.window_size :]

        # 检查连续重复的模式
        for length in range(1, len(recent_patterns) // 2 + 1):
            # 检查最后 2*length 是否是重复模式
            if len(recent_patterns) >= 2 * length:
                pattern1 = recent_patterns[-2 * length : -length]
                pattern2 = recent_patterns[-length:]

                # 如果忽略空值，过滤掉
                if ignore_empty:
                    pattern1 = [p for p in pattern1 if p]
                    pattern2 = [p for p in pattern2 if p]

                if pattern1 and pattern1 == pattern2:
                    return LoopDetectionResult(
                        is_loop=True,
                        confidence=0.85,
                        loop_length=length,
                        similar_turns=list(
                            range(len(recent_patterns) - 2 * length, len(recent_patterns))
                        ),
                        reason=f"{name}模式重复（周期长度 {length}）",
                    )

        return LoopDetectionResult(
            is_loop=False, confidence=0.0, loop_length=0, similar_turns=[], reason=""
        )

    @staticmethod
    def _hash(text: str) -> str:
        """计算文本的哈希值（用于快速比较）"""
        return hashlib.md5(text.encode()).hexdigest()[:8]

    def reset(self) -> None:
        """重置检测器状态"""
        self.turn_hashes.clear()
        self.tool_patterns.clear()
        self.error_messages.clear()
        self.thought_hashes.clear()
        self.turn_count = 0

    def stats(self) -> dict:
        """获取统计信息"""
        return {
            "turn_count": self.turn_count,
            "window_size": self.window_size,
            "similarity_threshold": self.similarity_threshold,
            "enabled": self.enabled,
            "hashes_stored": len(self.turn_hashes),
        }

    def __repr__(self) -> str:
        return (
            f"LoopDetector("
            f"turns={self.turn_count}, "
            f"window={self.window_size}, "
            f"threshold={self.similarity_threshold})"
        )
