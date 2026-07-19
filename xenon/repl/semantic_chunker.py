"""
v0.5.0: 语义分块器。

将对话轮次按语义关系分组成 'SemanticChunk'，使压缩操作
以块为单位进行（而非单个 turn）。核心模式：

    tool_call → tool_result → assistant_analysis  → 一个原子块

压缩时，整个块被统一处理：要么全保留，要么压缩为一条摘要。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SemanticChunk:
    """一个语义块 — 逻辑上相关的一组对话轮次。

    典型结构：
    - 单轮：user_input → assistant_output
    - 工具链：tool_call → tool_result → assistant_analysis
    - 混合：user_input → tool_call → tool_result → assistant_output
    """

    group_id: str
    turns: list = field(default_factory=list)
    dominant_tier: int = 3           # 块内最高 tier
    chunk_type: str = "general"      # single_turn | tool_chain | mixed | system
    summary: str = ""                # 压缩后的摘要（压缩后填充）

    @property
    def size(self) -> int:
        """块中的轮次数。"""
        return len(self.turns)

    @property
    def estimated_tokens(self) -> int:
        """块内所有 turn 的 token 估算之和。"""
        return sum(getattr(t, "token_count", 0) for t in self.turns)

    @property
    def has_tool_calls(self) -> bool:
        """块是否包含工具调用。"""
        return any(
            getattr(t, "turn_type", "general") in ("tool_call", "tool_result")
            for t in self.turns
        )

    @property
    def is_atomic(self) -> bool:
        """是否需要作为原子单元处理（工具链块必须原子处理）。"""
        return self.chunk_type in ("tool_chain", "mixed")

    def key_turns(self) -> list:
        """返回块中最重要的轮次（用于摘要生成）。"""
        # 优先返回 user_input 和 assistant_output，跳过工具结果
        key = []
        for t in self.turns:
            tt = getattr(t, "turn_type", "general")
            if tt in ("user_input", "assistant_output", "system"):
                key.append(t)
        if not key:
            # 如果只有工具调用和结果，取首尾
            if len(self.turns) >= 1:
                key.append(self.turns[0])
            if len(self.turns) >= 2:
                key.append(self.turns[-1])
        return key


class SemanticChunker:
    """将 ConversationTurn 列表分组成 SemanticChunk 列表。

    识别模式：
    1. tool_call 后紧跟 tool_result + assistant → tool_chain 块
    2. 连续的 assistant 消息（多轮分析） → 合并到同一个块
    3. 孤立的 system 消息 → 独立 system 块
    4. user_input + 直接 assistant_output → single_turn 块

    用法::

        chunker = SemanticChunker()
        chunks = chunker.group(turns)
        for chunk in chunks:
            print(f"{chunk.group_id}: {chunk.chunk_type} ({chunk.size} turns)")
    """

    def group(self, turns: list) -> list[SemanticChunk]:
        """将 turns 分组成语义块。"""
        if not turns:
            return []

        chunks: list[SemanticChunk] = []
        current: SemanticChunk | None = None
        group_counter = 0

        for turn in turns:
            tt = getattr(turn, "turn_type", "general")
            role = getattr(turn, "role", "")

            if current is None:
                # 开始新块
                group_counter += 1
                current = SemanticChunk(
                    group_id=f"sg-{group_counter}",
                    chunk_type=self._classify_start(turn),
                )
                current.turns.append(turn)
                current.dominant_tier = getattr(turn, "task_tier", 3)
            elif self._should_merge(current, turn):
                # 合并到当前块
                current.turns.append(turn)
                # 更新主导 tier（取最高）
                ttier = getattr(turn, "task_tier", 3)
                if ttier > current.dominant_tier:
                    current.dominant_tier = ttier
            else:
                # 结束当前块，开始新块
                chunks.append(current)
                group_counter += 1
                current = SemanticChunk(
                    group_id=f"sg-{group_counter}",
                    chunk_type=self._classify_start(turn),
                )
                current.turns.append(turn)
                current.dominant_tier = getattr(turn, "task_tier", 3)

        if current is not None and current.turns:
            chunks.append(current)

        # 后处理：标记 mixed 类型
        for chunk in chunks:
            if chunk.chunk_type == "single_turn" and chunk.has_tool_calls:
                chunk.chunk_type = "tool_chain"
            elif chunk.size > 3 and chunk.has_tool_calls:
                chunk.chunk_type = "mixed"

        return chunks

    def _classify_start(self, turn) -> str:
        """根据首轮类型分类块。"""
        tt = getattr(turn, "turn_type", "general")
        role = getattr(turn, "role", "")
        if role == "system" or tt == "system":
            return "system"
        if tt in ("tool_call", "tool_result"):
            return "tool_chain"
        return "single_turn"

    @staticmethod
    def _should_merge(current: SemanticChunk, next_turn) -> bool:
        """判断是否应该将 next_turn 合并到 current 块中。"""
        next_type = getattr(next_turn, "turn_type", "general")
        next_role = getattr(next_turn, "role", "general")

        # System 消息始终独立
        if next_role == "system" or next_type == "system":
            return False

        # 如果当前块最后一个 turn 是 tool_call 或 assistant_output
        # 且下一个是 tool_result 或 assistant → 合并（工具链）
        last_turns = current.turns[-2:] if current.size >= 2 else current.turns
        for lt in last_turns:
            lt_type = getattr(lt, "turn_type", "general")
            # tool_call → tool_result 合并
            if lt_type == "tool_call" and next_type == "tool_result":
                return True
            # tool_result → assistant_output 合并
            if lt_type == "tool_result" and next_type == "assistant_output":
                return True
            # assistant_output 可能包含更多 tool_calls
            if lt_type == "assistant_output" and next_type == "tool_call":
                return True
            # user_input → assistant_output 合并（简单问答）
            if lt_type == "user_input" and next_type == "assistant_output":
                return True
            # user_input → tool_call 合并（用户指令启动工具链）
            if lt_type == "user_input" and next_type == "tool_call":
                return True

        # 连续的 assistant 消息合并
        if lt_type == "assistant_output" and next_type == "assistant_output":
            return True

        return False

    def compress_chunk(
        self,
        chunk: SemanticChunk,
        strategy: Any = None,
    ) -> Any | None:
        """将整个语义块压缩为一条汇总消息。

        Args:
            chunk: 要压缩的语义块。
            strategy: CompressionStrategy（可选，用于参数控制）。

        Returns:
            一个 ConversationTurn 汇总消息，或 None（如果块为空）。
        """
        if not chunk.turns:
            return None

        # 获取 ConversationTurn 类
        TurnClass = type(chunk.turns[0])

        if not chunk.has_tool_calls:
            # 简单块：保留首尾轮次，中间用摘要替代
            if chunk.size <= 2:
                return None  # 不需要压缩
            key = chunk.key_turns()
            summary_content = (
                f"[语义块 {chunk.group_id}] "
                f"包含 {chunk.size} 轮对话，类型: {chunk.chunk_type}"
            )
            return TurnClass(
                role="system",
                content=summary_content,
                task_tier=chunk.dominant_tier,
                turn_type="system",
            )
        else:
            # 工具链块：生成工具摘要
            tool_names = []
            for t in chunk.turns:
                tt = getattr(t, "turn_type", "")
                if tt == "tool_call":
                    tool_names.append(_extract_tool_name_from_turn(t))
                elif tt == "tool_result":
                    tool_names.append(_extract_tool_name_from_turn(t) + "_result")

            tool_list = ", ".join(list(dict.fromkeys(tool_names))[:10])  # 去重保序
            summary_content = (
                f"[工具链块 {chunk.group_id}] "
                f"涉及工具: {tool_list}，共 {chunk.size} 轮交互"
            )
            return TurnClass(
                role="system",
                content=summary_content,
                task_tier=chunk.dominant_tier,
                turn_type="system",
            )

    def build_block_map(self, turns: list) -> dict[str, list[int]]:
        """构建 group_id → 原始索引列表的映射。

        用于在压缩后定位原始轮次位置。
        """
        chunks = self.group(turns)
        result: dict[str, list[int]] = {}
        offset = 0
        for chunk in chunks:
            indices = list(range(offset, offset + chunk.size))
            result[chunk.group_id] = indices
            offset += chunk.size
        return result


def _extract_tool_name_from_turn(turn) -> str:
    """从 turn 中提取工具名称。"""
    meta = getattr(turn, "metadata", {}) or {}
    if "tool_name" in meta:
        return str(meta["tool_name"])
    content = getattr(turn, "content", "")
    # 尝试从 content 模式匹配
    import re
    # 匹配 "tool_name:" 或 "调用 tool_name" 等模式
    m = re.search(r"(?:调用|执行|使用)\s*(\w+)", content)
    if m:
        return m.group(1)
    # 从 turn_type 的 turn 中提取
    tt = getattr(turn, "turn_type", "")
    if tt == "tool_call":
        return "tool_call"
    if tt == "tool_result":
        return "tool_result"
    return "unknown"
