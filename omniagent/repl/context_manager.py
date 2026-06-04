"""
Context Manager — 对话历史与 Token 管理。

职责：
1. 维护多轮对话的 message history。
2. 估算 token 用量（基于词数的粗略估算）。
3. 支持 /compact 压缩：将旧对话摘要化，释放 context window。
4. 支持 /undo 回退：撤销最近一轮对话。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConversationTurn:
    """一轮对话记录。"""

    role: str  # "user" | "assistant" | "system" | "tool"
    content: str
    model_used: str | None = None
    node_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ContextManager:
    """
    对话上下文管理器。

    管理 message history、token 估算、压缩和回退。
    """

    def __init__(self, *, max_tokens: int = 128000, compact_threshold: float = 0.8) -> None:
        self.max_tokens = max_tokens
        self.compact_threshold = compact_threshold  # 达到 max_tokens 的 80% 时提醒
        self.history: list[ConversationTurn] = []
        self._undo_stack: list[list[ConversationTurn]] = []
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0

    # ── 对话管理 ──────────────────────────────────────────

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """添加一条消息到历史。"""
        turn = ConversationTurn(role=role, content=content, **kwargs)
        self.history.append(turn)

    def add_user_message(self, content: str) -> None:
        self.add_message("user", content)

    def add_assistant_message(self, content: str, *, model_used: str | None = None) -> None:
        self.add_message("assistant", content, model_used=model_used)

    def add_system_message(self, content: str) -> None:
        self.add_message("system", content)

    def get_messages(self) -> list[dict[str, str]]:
        """将历史转换为 LLM API 所需的 messages 格式。"""
        return [{"role": turn.role, "content": turn.content} for turn in self.history]

    # ── Token 估算 ────────────────────────────────────────

    def estimate_tokens(self, text: str) -> int:
        """
        估算 token 数。
        规则：
        - 中文字符约 2 token/字
        - 英文约 1 token/4 字符（即 0.25 token/char）
        - 代码/JSON 密度更高
        - 始终不低于 len(text)/4（防止无空格长串被低估）
        """
        if not text:
            return 0

        # 统计中文字符数
        cjk_count = sum(1 for c in text if '一' <= c <= '鿿')
        # 英文单词数
        words = len(text.split())
        # 总字符数
        chars = len(text)

        # 检测是否包含大量代码/JSON
        code_chars = text.count('{') + text.count('}') + text.count(';') + text.count('=')
        is_code_heavy = code_chars > chars * 0.02

        # 基础估算：至少 len/2（防止无空格长串被低估）
        char_based = max(chars // 2, 1)

        if is_code_heavy:
            return max(words * 2, int(chars * 0.4))
        elif cjk_count > chars * 0.3:
            return max(words, int(cjk_count * 2), char_based)
        else:
            return max(words, int(words * 1.3), char_based)

    def current_token_usage(self) -> int:
        """估算当前历史的总 token 数。"""
        return sum(self.estimate_tokens(turn.content) for turn in self.history)

    def usage_ratio(self) -> float:
        """当前 token 使用率 (0.0 ~ 1.0+)。"""
        return self.current_token_usage() / self.max_tokens if self.max_tokens > 0 else 0.0

    def needs_compact(self) -> bool:
        """是否需要压缩。"""
        return self.usage_ratio() >= self.compact_threshold

    # ── /undo 回退 ────────────────────────────────────────

    def save_snapshot(self) -> None:
        """保存当前历史快照（用于 undo）。"""
        self._undo_stack.append(copy.deepcopy(self.history))

    def undo(self) -> bool:
        """
        回退到上一个快照。

        Returns:
            True 如果成功回退，False 如果没有可回退的快照。
        """
        if not self._undo_stack:
            return False
        self.history = self._undo_stack.pop()
        return True

    @property
    def undo_depth(self) -> int:
        return len(self._undo_stack)

    # ── /compact 压缩 ────────────────────────────────────

    def compact(self, summary: str | None = None, model_priority: list[str] | None = None) -> str:
        """
        压缩对话历史。

        策略：保留最近 3 轮对话完整，压缩更早的消息为摘要。
        这样既节省 Token，又保留近期上下文的连贯性。

        Args:
            summary: 手动提供的摘要
            model_priority: 用于 LLM 摘要的模型列表

        Returns:
            压缩后的摘要文本。
        """
        # 找到最近 3 轮对话的分界点（按 user 消息计数）
        keep_rounds = 3
        user_count = 0
        cut_idx = 0
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i].role == "user":
                user_count += 1
                if user_count >= keep_rounds:
                    cut_idx = i
                    break

        # 如果历史很短且没有手动摘要，提示无需压缩
        if cut_idx == 0 and len(self.history) <= 6 and not summary:
            return "（对话历史较短，无需压缩）"

        older = self.history[:cut_idx]
        recent = self.history[cut_idx:]

        # 对更早的消息生成摘要
        if not summary:
            summary = self._llm_summary(model_priority, messages=older) if model_priority else self._auto_summary(messages=older)

        # 保存快照以便 undo
        self.save_snapshot()

        # 替换历史：摘要 + 最近 3 轮完整对话
        old_count = len(older) if older else len(self.history)
        self.history = [
            ConversationTurn(
                role="system",
                content=f"[对话历史已压缩] 以下是之前 {old_count} 条消息的摘要：\n\n{summary}",
            )
        ] + recent

        return summary

    def _llm_summary(self, model_priority: list[str], messages: list | None = None) -> str:
        """使用 LLM 生成对话摘要。"""
        try:
            from omniagent.utils.llm_client import chat_completion

            target = messages or self.history
            recent = target[-20:]  # 最多取 20 条
            conversation = "\n".join(
                f"[{t.role}] {t.content[:300]}" for t in recent
            )

            msgs = [
                {"role": "system", "content": "请用中文简洁地总结以下对话的要点，保留关键信息、代码片段和技术决策。不超过 500 字。"},
                {"role": "user", "content": conversation},
            ]

            for model_id in model_priority:
                try:
                    return chat_completion(model_id, msgs, max_tokens=800, temperature=0.3)
                except Exception:
                    continue

            return self._auto_summary(messages=messages)

        except Exception:
            return self._auto_summary(messages=messages)

    def _auto_summary(self, messages: list | None = None) -> str:
        """自动生成摘要（简单实现：保留关键信息）。"""
        target = messages or self.history
        user_msgs = [t for t in target if t.role == "user"]
        assistant_msgs = [t for t in target if t.role == "assistant"]

        parts = []
        if user_msgs:
            recent_user = user_msgs[-3:]
            parts.append("用户最近的请求:")
            for msg in recent_user:
                parts.append(f"  - {msg.content[:200]}")

        if assistant_msgs:
            recent_asst = assistant_msgs[-2:]
            parts.append("助手最近的回复:")
            for msg in recent_asst:
                parts.append(f"  - {msg.content[:300]}")

        return "\n".join(parts) if parts else "（无对话内容）"

    # ── 统计 ──────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """返回当前上下文统计信息。"""
        return {
            "total_messages": len(self.history),
            "user_messages": sum(1 for t in self.history if t.role == "user"),
            "assistant_messages": sum(1 for t in self.history if t.role == "assistant"),
            "system_messages": sum(1 for t in self.history if t.role == "system"),
            "estimated_tokens": self.current_token_usage(),
            "max_tokens": self.max_tokens,
            "usage_ratio": f"{self.usage_ratio():.1%}",
            "undo_available": self.undo_depth,
            "needs_compact": self.needs_compact(),
        }

    def clear(self) -> None:
        """清空所有历史。"""
        self.save_snapshot()
        self.history.clear()
