"""
v0.4.0: Task difficulty estimator.

Extends detect_intent's 11-category regex classifier with
quantitative complexity scoring. Outputs TaskProfile for AutoRouter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class TaskProfile:
    """Task profile used by AutoRouter for model selection."""
    intent: str | None = None
    complexity: float = 0.3
    requires_reasoning: bool = False
    requires_code_generation: bool = False
    requires_tools: bool = False
    requires_long_context: bool = False
    estimated_tokens: int = 0
    expects_streaming: bool = True


class DifficultyEstimator:
    """Analyzes user input and produces a TaskProfile."""

    def estimate(
        self, user_input: str, context_messages: list[dict] | None = None,
    ) -> TaskProfile:
        context = context_messages or []
        intent = self._detect_intent(user_input)
        complexity = self._measure_complexity(user_input, intent)
        requires_tools = self._needs_tools(user_input, intent)
        requires_code = intent in (
            "write_code", "convert", "refactor", "debug", "write_test",
        )
        requires_reasoning = intent in (
            "debug", "design", "refactor", "novel", "write_test", "explain",
        )
        est_tokens = self._estimate_tokens(user_input, context)

        return TaskProfile(
            intent=intent, complexity=complexity,
            requires_reasoning=requires_reasoning,
            requires_code_generation=requires_code,
            requires_tools=requires_tools,
            estimated_tokens=est_tokens,
            requires_long_context=est_tokens > 32000,
            expects_streaming=True,
        )

    @staticmethod
    def _detect_intent(text: str) -> str | None:
        from xenon.repl.prompt_optimizer import detect_intent
        return detect_intent(text)

    @staticmethod
    def _measure_complexity(text: str, intent: str | None) -> float:
        score = 0.3
        intent_base = {
            "chat": 0.05, "query": 0.1, "explain": 0.3,
            "write_code": 0.5, "convert": 0.5, "write_test": 0.5,
            "debug": 0.6, "refactor": 0.6, "write_doc": 0.4,
            "design": 0.7, "novel": 0.6,
        }
        score += intent_base.get(intent, 0.3)
        if re.search(r"(?:多步|逐步|迭代|反复|多个文件|整个|项目|工程|系统|重构|重写|迁移|改造)", text):
            score += 0.15
        if re.search(r"(?:性能|优化|安全|并发|分布式|架构|设计模式)", text):
            score += 0.15
        if re.search(r"(?:复杂|困难|很难|挑战|大规模)", text):
            score += 0.1
        # v0.5.6: 更细致的长度感知
        text_len = len(text)
        if text_len > 200:
            score += 0.05
        if text_len > 500:
            score += 0.08
        if text_len > 1000:
            score += 0.1
        # v0.5.6: 有换行/分段说明用户花了心思，任务可能更细
        if "\n" in text:
            score += 0.03
        # v0.5.6: 代码块标记说明有代码要处理
        if "```" in text:
            score += 0.05
        file_refs = len(re.findall(r"\b\w+\.(?:py|js|ts|java|go|rs)\b", text))
        score += min(file_refs * 0.05, 0.15)
        return min(score, 1.0)

    @staticmethod
    def estimate_tier(profile: TaskProfile) -> int:
        """从 TaskProfile 估计任务层级 (1-5)，用于多优先级队列调度。

        - complexity ≤ 0.2 → tier 1 (琐碎：问候、简单查询)
        - complexity ≤ 0.4 → tier 2 (轻量：解释、翻译)
        - complexity ≤ 0.6 → tier 3 (标准：代码生成、调试)
        - complexity ≤ 0.8 → tier 4 (复杂：重构、多文件)
        - complexity > 0.8 → tier 5 (旗舰：架构设计)
        """
        c = profile.complexity
        if c <= 0.2:
            tier = 1
        elif c <= 0.4:
            tier = 2
        elif c <= 0.6:
            tier = 3
        elif c <= 0.8:
            tier = 4
        else:
            tier = 5

        # 需要推理的任务至少升至 tier 3
        if profile.requires_reasoning and tier < 3:
            tier = 3
        # 需要代码生成且复杂度高，升至 tier 4
        if profile.requires_code_generation and c > 0.5 and tier < 4:
            tier = 4

        return tier

    @staticmethod
    def _needs_tools(text: str, intent: str | None) -> bool:
        if intent in ("query", "write_code"):
            return True
        from xenon.repl.repl import REPL
        return REPL._detect_tool_need(text, intent=intent)

    @staticmethod
    def _estimate_tokens(
        user_input: str, context_messages: list[dict],
    ) -> int:
        total = len(user_input)
        for m in context_messages:
            total += len(m.get("content", ""))
        return total // 2
