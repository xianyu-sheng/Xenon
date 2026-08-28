"""
范式选择器 - 基于规则引擎选择最优推理范式

根据任务特征，通过优先级规则匹配选择最合适的推理范式。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from xenon.repl.feature_extractor import TaskFeatures

logger = logging.getLogger(__name__)


@dataclass
class RoutingDecision:
    """路由决策结果"""
    paradigm: str               # 选择的范式名称
    confidence: float           # 置信度 (0-1)
    reason: str                 # 选择原因（用户可见）
    matched_rule: str | None = None  # 命中的规则ID


@dataclass
class RoutingRule:
    """路由规则"""
    rule_id: str
    description: str
    condition: Callable[[TaskFeatures], bool]
    paradigm: str
    confidence: float
    reason: str
    priority: int = 0  # 优先级，数字越大越优先


class ParadigmSelector:
    """
    范式选择器 - 基于规则引擎

    维护一组优先级规则，按优先级顺序匹配，返回第一个命中的规则。
    """

    def __init__(self):
        self.rules: list[RoutingRule] = []
        self._init_builtin_rules()

    def _init_builtin_rules(self):
        """初始化内置规则库"""

        # 规则优先级：特殊情况 (90-100) > 一般情况 (40-70) > 兜底规则 (10-30)

        # ===== 高优先级规则 (特殊情况) =====

        # 规则R1: 纯查询任务 → direct
        self.add_rule(RoutingRule(
            rule_id="R1",
            description="纯查询任务使用 direct 模式",
            condition=lambda f: (
                f.is_query
                and not f.has_file_operations
                and f.estimated_tool_calls == 0
            ),
            paradigm="direct",
            confidence=0.9,
            reason="这是一个简单的查询问题，不需要工具调用",
            priority=100
        ))

        # 规则R2: 质量关键的代码生成 → reflection
        self.add_rule(RoutingRule(
            rule_id="R2",
            description="质量敏感的代码任务使用 reflection",
            condition=lambda f: (
                f.is_coding
                and f.quality_critical
            ),
            paradigm="reflection",
            confidence=0.85,
            reason="代码质量要求高，使用 Reflection 模式进行自我审查",
            priority=90
        ))

        # 规则R3: 探索性 + 质量要求 → react-reflection
        self.add_rule(RoutingRule(
            rule_id="R3",
            description="探索性且质量敏感任务使用 react-reflection",
            condition=lambda f: (
                f.is_exploratory
                and f.quality_critical
                and f.estimated_tool_calls > 2
            ),
            paradigm="react-reflection",
            confidence=0.8,
            reason="任务需要探索并保证质量，使用 ReAct+Reflection 组合",
            priority=85
        ))

        # ===== 中优先级规则 (一般情况) =====

        # 规则R4: 多工具调用 → react
        self.add_rule(RoutingRule(
            rule_id="R4",
            description="需要多次工具调用使用 react",
            condition=lambda f: f.estimated_tool_calls >= 3,
            paradigm="react",
            confidence=0.75,
            reason="任务需要多次工具调用，使用 ReAct 循环处理",
            priority=70
        ))

        # 规则R5: 高复杂度多步骤 → plan-execute
        self.add_rule(RoutingRule(
            rule_id="R5",
            description="复杂多步骤任务使用 plan-execute",
            condition=lambda f: (
                f.complexity_score > 0.6
                and f.estimated_steps >= 4
            ),
            paradigm="plan-execute",
            confidence=0.7,
            reason="任务复杂且步骤较多，先规划再执行更稳妥",
            priority=60
        ))

        # 规则R6: 代码生成 (非关键) → react
        self.add_rule(RoutingRule(
            rule_id="R6",
            description="一般代码生成使用 react",
            condition=lambda f: (
                f.is_coding
                and not f.quality_critical
            ),
            paradigm="react",
            confidence=0.65,
            reason="代码生成可能需要读写文件和测试，使用 ReAct 模式",
            priority=50
        ))

        # 规则R7: 文件操作任务 → react
        self.add_rule(RoutingRule(
            rule_id="R7",
            description="文件操作任务使用 react",
            condition=lambda f: f.has_file_operations,
            paradigm="react",
            confidence=0.6,
            reason="任务涉及文件操作，使用 ReAct 模式便于调试",
            priority=40
        ))

        # ===== 低优先级规则 (兜底) =====

        # 规则R8: 中等复杂度 → react
        self.add_rule(RoutingRule(
            rule_id="R8",
            description="中等复杂度任务使用 react",
            condition=lambda f: (
                0.3 < f.complexity_score <= 0.6
            ),
            paradigm="react",
            confidence=0.55,
            reason="任务有一定复杂度，使用 ReAct 模式更灵活",
            priority=30
        ))

        # 规则R9: 默认简单任务 → direct
        self.add_rule(RoutingRule(
            rule_id="R9",
            description="简单任务默认使用 direct",
            condition=lambda f: f.complexity_score <= 0.3,
            paradigm="direct",
            confidence=0.5,
            reason="任务较简单，直接对话即可",
            priority=10
        ))

    def add_rule(self, rule: RoutingRule) -> None:
        """添加路由规则并按优先级排序"""
        self.rules.append(rule)
        # 按优先级排序（高优先级在前）
        self.rules.sort(key=lambda r: r.priority, reverse=True)

    def select(self, features: TaskFeatures) -> RoutingDecision:
        """
        基于特征选择范式

        返回第一个匹配的规则，如果没有匹配则返回 direct（兜底）
        """
        for rule in self.rules:
            try:
                if rule.condition(features):
                    logger.info(
                        f"路由规则命中: [{rule.rule_id}] {rule.description} "
                        f"→ {rule.paradigm} (置信度: {rule.confidence})"
                    )
                    return RoutingDecision(
                        paradigm=rule.paradigm,
                        confidence=rule.confidence,
                        reason=rule.reason,
                        matched_rule=rule.rule_id
                    )
            except Exception as e:
                logger.warning(f"规则 [{rule.rule_id}] 执行失败: {e}")
                continue

        # 兜底：没有任何规则命中，返回 direct
        logger.warning("没有规则命中，回落到 direct 模式")
        return RoutingDecision(
            paradigm="direct",
            confidence=0.3,
            reason="未找到匹配规则，使用默认模式",
            matched_rule=None
        )

    def list_rules(self) -> list[RoutingRule]:
        """列出所有规则（按优先级排序）"""
        return list(self.rules)
