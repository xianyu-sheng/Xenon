"""结构化上下文压缩器 — 借鉴 KamaClaude 的 6 段压缩设计。

将长对话历史压缩为结构化摘要，包含:
1. Original Goal — 原始目标
2. Completed Steps — 已完成步骤（含文件路径、命令）
3. Key Constraints & Discoveries — 关键约束和发现
4. Current File State — 当前文件状态
5. Remaining TODOs — 待完成任务
6. Critical Data — 关键数据（ID、token、错误信息等）

触发条件:
- 上下文超过 compact_threshold（默认 80% context window）
- 手动 /compact 命令
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_COMPACT_PROMPT = """\
You are compressing an agent conversation into a handoff summary.
Another LLM instance will continue this task from your summary alone — make it complete.

Structure your response with exactly these six sections:

## 1. Original Goal
One sentence describing what the user asked the agent to accomplish.

## 2. Completed Steps
Bullet list of what has been done. Be specific (file paths, commands run, decisions made).

## 3. Key Constraints & Discoveries
Facts learned during the run that affect future decisions \
(e.g., API limitations, file formats, user preferences stated mid-conversation).

## 4. Current File State
For each file that was created or modified: path, a one-line description of its current state.

## 5. Remaining TODOs
Ordered list of what still needs to be done to complete the original goal.

## 6. Critical Data
Any values the next LLM needs verbatim: IDs, tokens, exact error messages, config values \
discovered during the run.

Be concise. Omit reasoning steps and intermediate attempts. Keep conclusions.\
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ts_compact() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


@dataclass
class CompactionResult:
    """压缩结果。"""
    summary_text: str
    original_token_estimate: int
    summary_tokens: int


class Compactor:
    """结构化上下文压缩器。

    使用方式:
        compactor = Compactor(session_dir=Path(".omniagent/sessions/sess-xxx"))
        result = await compactor.compact(messages, provider)
        if result:
            messages = compacted_messages
    """

    def __init__(
        self,
        session_dir: Path,
        *,
        compact_threshold: float = 0.80,
        context_window: int = 200_000,
    ) -> None:
        self._session_dir = session_dir
        self._compact_threshold = compact_threshold
        self._context_window = context_window

    def needs_compact(self, estimated_tokens: int) -> bool:
        """检查是否需要压缩。"""
        return estimated_tokens > self._context_window * self._compact_threshold

    def compact(
        self,
        messages: list[dict[str, Any]],
        model_priority: list[str],
        *,
        focus: str = "",
        max_tokens: int = 4096,
    ) -> CompactionResult | None:
        """压缩消息列表，返回 CompactionResult 或 None（失败时）。

        Args:
            messages: 待压缩的消息列表
            model_priority: LLM 模型优先级列表
            focus: 可选的压缩焦点
            max_tokens: 压缩摘要的最大 token 数
        """

        # 估算 token 数
        original_estimate = self._estimate_tokens(messages)
        if original_estimate < self._context_window * 0.5:
            return None  # 上下文还不大，不需要压缩

        # 构建压缩请求
        compact_prompt = _COMPACT_PROMPT
        if focus:
            compact_prompt += f"\nFocus on: {focus}"

        # 将消息列表格式化为文本
        conversation_text = self._format_messages(messages)
        compact_messages = [
            {"role": "system", "content": compact_prompt},
            {"role": "user", "content": f"Compress this conversation:\n\n{conversation_text}"},
        ]

        try:
            from omniagent.utils.llm_client import chat_completion

            summary = chat_completion(
                model_priority[0],
                compact_messages,
                max_tokens=max_tokens,
                temperature=0.3,
            )
        except Exception as e:
            # 尝试 fallback 模型
            fallback_error = e
            for model in model_priority[1:]:
                try:
                    summary = chat_completion(
                        model,
                        compact_messages,
                        max_tokens=max_tokens,
                        temperature=0.3,
                    )
                    break
                except Exception:
                    continue
            else:
                logger.warning(f"压缩失败（所有模型均失败）: {fallback_error}")
                return None

        summary_tokens = self._estimate_tokens_from_text(summary)

        # 保存压缩摘要到 session 目录
        self._session_dir.mkdir(parents=True, exist_ok=True)
        summary_path = self._session_dir / f"compact-{_ts_compact()}.md"
        summary_path.write_text(summary, encoding="utf-8")

        logger.info(
            f"上下文压缩: original≈{original_estimate} summary={summary_tokens} tokens "
            f"(节省 {original_estimate - summary_tokens})"
        )

        return CompactionResult(
            summary_text=summary,
            original_token_estimate=original_estimate,
            summary_tokens=summary_tokens,
        )

    def apply_compact(
        self, messages: list[dict[str, Any]], result: CompactionResult,
    ) -> list[dict[str, Any]]:
        """将压缩结果应用到消息列表（就地替换）。"""
        return [
            {"role": "user", "content": result.summary_text},
            {"role": "assistant", "content": "Understood, I'll continue from this summary."},
        ]

    # ── 工具方法 ────────────────────────────────────────────

    @staticmethod
    def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
        """估算消息列表的 token 数（粗略估算: 1 token ≈ 4 字符）。"""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content) // 4
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        for v in block.values():
                            if isinstance(v, str):
                                total += len(v) // 4
        return total

    @staticmethod
    def _estimate_tokens_from_text(text: str) -> int:
        return len(text) // 4

    @staticmethod
    def _format_messages(messages: list[dict[str, Any]]) -> str:
        """将消息列表格式化为文本。"""
        lines = []
        for msg in messages[-50:]:  # 只取最近 50 条
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append(str(block.get("text", "")))
                        elif block.get("type") == "tool_use":
                            parts.append(f"[tool: {block.get('name', '?')}]")
                        elif block.get("type") == "tool_result":
                            result = str(block.get("content", ""))
                            parts.append(f"[result: {result[:100]}]")
                text = "\n".join(parts)
            else:
                text = str(content)

            if len(text) > 500:
                text = text[:500] + "..."
            lines.append(f"[{role}] {text}")

        return "\n".join(lines)
