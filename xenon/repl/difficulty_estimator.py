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
    # 推荐范式与置信度。此前范式只能由用户 /mode 手动切换，导致多步骤任务
    # 也跑在 direct/react 上——plan-execute、reflection 等引擎几乎从未被用到。
    # confidence 低于 REPL 的阈值时不自动切换，保留用户的显式选择。
    recommended_engine: str = "direct"
    engine_confidence: float = 0.0
    engine_reason: str = ""


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

        engine, confidence, engine_reason = self._recommend_engine(
            user_input,
            intent=intent,
            complexity=complexity,
            requires_tools=requires_tools,
            requires_reasoning=requires_reasoning,
            context_messages=context,
        )

        return TaskProfile(
            intent=intent, complexity=complexity,
            requires_reasoning=requires_reasoning,
            requires_code_generation=requires_code,
            requires_tools=requires_tools,
            estimated_tokens=est_tokens,
            requires_long_context=est_tokens > 32000,
            expects_streaming=True,
            recommended_engine=engine,
            engine_confidence=confidence,
            engine_reason=engine_reason,
        )

    @staticmethod
    def _detect_intent(text: str) -> str | None:
        from xenon.repl.prompt_optimizer import detect_intent
        return detect_intent(text)

    @staticmethod
    def _measure_complexity(text: str, intent: str | None) -> float:
        # 空/纯空白输入没有任何任务信号，按最低复杂度处理——否则
        # 空串会落到 intent_base 的默认 0.3 + 基础 0.3 = 0.6（tier 3），
        # 把「无任务」调度成「标准任务」。
        if not text or not text.strip():
            return 0.1
        score = 0.3
        intent_base = {
            "chat": 0.05, "query": 0.1, "research": 0.3, "explain": 0.3,
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
        # v0.5.6: 更细致的长度感知。长度是「信息密度」的弱信号——
        # 重复字符/刷屏内容（如 "x"*100000）长度大但信息量为零，
        # 不能按长文处理加分，否则噪声输入被调度成 tier 5 困难任务。
        text_len = len(text)
        is_repetitive = text_len > 200 and len(set(text)) <= 4
        if not is_repetitive:
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
    def _recommend_engine(
        text: str,
        *,
        intent: str | None,
        complexity: float,
        requires_tools: bool,
        requires_reasoning: bool,
        context_messages: list[dict],
    ) -> tuple[str, float, str]:
        """按任务结构特征推荐范式，返回 (engine, confidence, reason)。

        判据是**语言结构与任务规模**，不枚举业务领域：多阶段连接词说明任务
        可分解（plan-execute 的强项），质量诉求说明需要自审（reflection），
        多轮 + 需要工具说明是探索式修改（react）。置信度表达"有多确定该换"，
        由 REPL 决定是否越过用户的显式 /mode 选择。

        不需要工具的任务一律留在 direct：范式引擎的价值全在工具循环上，
        纯问答套上 plan/reflect 只是白烧 token。
        """
        if not text or not text.strip():
            return "direct", 0.0, ""
        if not requires_tools:
            return "direct", 0.0, "无需工具，纯对话即可完成"

        # 多阶段结构：顺序连接词，或明确的批量/全局作用域。
        multi_stage = bool(
            re.search(
                r"(?:然后|接着|之后|再(?:去|把|将)|最后|首先|第一步|第二步|下一步)"
                r"|(?:先).{0,20}(?:再|然后|后)"
                r"|(?:多个|所有|全部|逐个|批量|整个|全项目|全仓库)"
                r"(?:文件|模块|函数|类|目录|测试|接口)"
                r"|(?:迁移|重写|重构|升级|改造|统一|规范化)"
                r"(?:整个|全部|所有|一下)?(?:项目|仓库|代码库|模块|架构|系统)"
                r"|(?:first).{0,30}(?:then|after)"
                r"|\bstep\s*\d|\bmigrat|\brewrite\s+(?:the\s+)?(?:whole|entire)",
                text,
                re.IGNORECASE,
            )
        )
        # 质量诉求：要求校验、审查或"确保正确"。
        wants_quality = bool(
            re.search(
                r"(?:检查|审查|复查|校验|核对|确保|保证|务必|不要(?:出错|遗漏))"
                r"|(?:高质量|健壮|完备|周全|无遗漏)"
                r"|(?:加|补)(?:上)?(?:单元)?测试"
                r"|(?:review|verify|validate|double[- ]check|make\s+sure)\b",
                text,
                re.IGNORECASE,
            )
        )

        if multi_stage and wants_quality:
            return (
                "plan-reflection", 0.75,
                "多阶段任务且有质量要求：规划执行后反思修正",
            )
        if multi_stage:
            return "plan-execute", 0.8, "任务可分解为多个阶段：先规划再逐步执行"
        # 质量诉求先于纯复杂度：用户说了"确保/加测试"，自审比全局规划更对症。
        # 反过来排会让所有带质量要求的高复杂度任务都被 plan-react 吞掉，
        # reflection 永远轮不到。
        if wants_quality and intent in {
            "write_code", "write_test", "write_doc", "refactor", "debug",
        }:
            return "reflection", 0.7, "有明确质量要求：执行后自我审查并修正"
        if complexity > 0.7 and requires_reasoning:
            return (
                "plan-react", 0.7,
                "高复杂度推理任务：全局规划 + 每步 ReAct 执行",
            )
        # 多轮对话里的工具任务通常是"改—验—再改"的探索循环。
        if len(context_messages) > 2 and complexity > 0.45:
            return "react", 0.6, "多轮探索式任务：思考-行动-观察循环"
        return "direct", 0.0, ""

    @staticmethod
    def _needs_tools(text: str, intent: str | None) -> bool:
        from xenon.repl.execution_policy import classify_execution_policy

        return classify_execution_policy(text, intent=intent).requires_tools

    @staticmethod
    def _estimate_tokens(
        user_input: str, context_messages: list[dict],
    ) -> int:
        total = len(user_input)
        for m in context_messages:
            total += len(m.get("content", ""))
        return total // 2
