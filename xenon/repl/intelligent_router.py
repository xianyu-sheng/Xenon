"""
智能路由器 - Xenon 推理范式自动选择

根据用户输入和上下文自动选择最合适的推理范式。
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

from xenon.repl.feature_extractor import FeatureExtractor
from xenon.repl.paradigm_selector import ParadigmSelector, RoutingDecision
from xenon.repl.routing_history import (
    DecisionHistory,
    OverrideRecord,
    ParadigmPreferences,
    RoutingDecisionRecord,
)

if TYPE_CHECKING:
    from xenon.repl.context_manager import ContextManager

logger = logging.getLogger(__name__)


class IntelligentRouter:
    """
    智能路由器主类

    协调特征提取和范式选择，提供统一的路由接口。
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        confidence_threshold: float = 0.6,
        notify_user: bool = True,
        switch_only_on_improvement: bool = True,
        decision_history: DecisionHistory | None = None,
        preferences: ParadigmPreferences | None = None,
    ):
        """
        初始化智能路由器

        Args:
            enabled: 是否启用智能路由
            confidence_threshold: 置信度阈值，低于此值不自动切换
            notify_user: 是否通知用户切换原因
            switch_only_on_improvement: 只在新范式"更好"时切换（避免频繁切换）
        """
        self.enabled = enabled
        self.confidence_threshold = confidence_threshold
        self.notify_user = notify_user
        self.switch_only_on_improvement = switch_only_on_improvement
        self.decision_history = decision_history or DecisionHistory()
        # In-memory by default (no persist path), so tests never touch disk.
        self.preferences = preferences or ParadigmPreferences()
        # (signature, recommended paradigm, input preview) of the latest route()
        self._last_recommendation: tuple[str, str, str] | None = None

        self.extractor = FeatureExtractor()
        self.selector = ParadigmSelector()

        # 统计信息
        self._total_routes = 0
        self._successful_routes = 0
        self._switches = 0

    def route(
        self,
        user_input: str,
        context: ContextManager,
        current_mode: str
    ) -> RoutingDecision | None:
        """
        路由决策主入口

        Args:
            user_input: 用户输入
            context: 对话上下文
            current_mode: 当前范式

        Returns:
            RoutingDecision: 如果建议切换范式
            None: 保持当前范式
        """
        if not self.enabled:
            return None

        self._total_routes += 1

        try:
            # Step 1: 提取任务特征
            features = self.extractor.extract(user_input, context)
            logger.debug(f"任务特征: {features}")

            # Step 2: 选择范式
            decision = self.selector.select(features)
            logger.info(
                f"路由决策: {decision.paradigm} "
                f"(置信度: {decision.confidence:.2f}, 规则: {decision.matched_rule})"
            )

            # Step 2.5: 用户偏好加权（小幅、封顶 1.0；不绕过后续任何检查）
            decision = self._apply_preference(decision, features, user_input)

            # Step 3: 判断是否需要切换
            if decision.paradigm == current_mode:
                # 与当前模式一致，无需切换
                logger.debug(f"推荐范式与当前一致: {current_mode}")
                self._record_decision(
                    user_input, features, decision, current_mode, switched=False
                )
                return None

            # Step 4: 置信度检查
            if decision.confidence < self.confidence_threshold:
                logger.info(
                    f"置信度 {decision.confidence:.2f} 低于阈值 "
                    f"{self.confidence_threshold}，不切换"
                )
                self._record_decision(
                    user_input, features, decision, current_mode, switched=False
                )
                return None

            # Step 5: 优先级检查（避免从强范式降级到弱范式）
            if self.switch_only_on_improvement:
                if not self._is_improvement(current_mode, decision.paradigm):
                    logger.info(
                        f"不建议从 {current_mode} 切换到 {decision.paradigm}，"
                        f"保持当前范式"
                    )
                    self._record_decision(
                        user_input, features, decision, current_mode, switched=False
                    )
                    return None

            # 通过所有检查，建议切换
            self._successful_routes += 1
            self._switches += 1
            self._record_decision(
                user_input, features, decision, current_mode, switched=True
            )
            return decision

        except Exception as e:
            logger.error(f"路由决策失败: {e}", exc_info=True)
            return None

    @staticmethod
    def _signature(features: object) -> str:
        """Coarse, low-cardinality task signature used to group overrides."""
        bucket = min(2, int(getattr(features, "complexity_score", 0.0) * 3))
        parts = [
            "coding" if getattr(features, "is_coding", False) else "",
            "query" if getattr(features, "is_query", False) else "",
            "explore" if getattr(features, "is_exploratory", False) else "",
            "quality" if getattr(features, "quality_critical", False) else "",
            "files" if getattr(features, "has_file_operations", False) else "",
            f"c{bucket}",
        ]
        return "+".join(p for p in parts if p)

    def _apply_preference(
        self, decision: RoutingDecision, features: object, user_input: str
    ) -> RoutingDecision:
        """Boost the recommended paradigm if the user historically prefers it.

        Remembers the recommendation so a later manual /mode can be recorded.
        Never raises: on any error the original decision is returned.
        """
        try:
            signature = self._signature(features)
            self._last_recommendation = (
                signature,
                decision.paradigm,
                user_input[:120],
            )
            boosted = self.preferences.weight(
                signature, decision.paradigm, decision.confidence
            )
            if boosted != decision.confidence:
                logger.debug(f"偏好加权: {decision.confidence:.2f} -> {boosted:.2f}")
                return replace(decision, confidence=boosted)
        except Exception:
            logger.debug("偏好加权失败（已忽略）", exc_info=True)
        return decision

    def record_override(self, chosen_mode: str) -> None:
        """Record that the user manually switched to ``chosen_mode``.

        Called from /mode. No-op without a pending recommendation or when the
        user simply picked what the router recommended. Never raises.
        """
        try:
            last = self._last_recommendation
            if last is None:
                return
            signature, recommended, preview = last
            self._last_recommendation = None
            if chosen_mode == recommended:
                return
            self.preferences.record(
                OverrideRecord(
                    timestamp=time.time(),
                    user_input_preview=preview,
                    signature=signature,
                    recommended=recommended,
                    chosen=chosen_mode,
                )
            )
        except Exception:
            logger.debug("记录范式覆盖失败（已忽略）", exc_info=True)

    def _record_decision(
        self,
        user_input: str,
        features: object,
        decision: RoutingDecision,
        current_mode: str,
        *,
        switched: bool,
    ) -> None:
        """Persist one bounded, privacy-safe decision record."""
        try:
            self.decision_history.record(
                RoutingDecisionRecord(
                    timestamp=time.time(),
                    user_input_preview=user_input[:120],
                    features=asdict(features),
                    matched_rule=decision.matched_rule,
                    recommended_paradigm=decision.paradigm,
                    confidence=decision.confidence,
                    current_paradigm=current_mode,
                    switched=switched,
                )
            )
        except Exception:
            logger.debug("记录智能路由决策失败（已忽略）", exc_info=True)

    def _is_improvement(self, current: str, proposed: str) -> bool:
        """
        判断新范式是否比当前范式"更好"

        避免频繁在相近范式间切换，以及从复杂范式降级到简单范式。

        范式强度排序（主观）:
        - direct < react < plan-execute ≈ reflection < plan-react ≈ react-reflection < plan-reflection
        """
        strength_order = {
            "direct": 1,
            "react": 2,
            "plan-execute": 3,
            "reflection": 3,
            "plan-react": 4,
            "react-reflection": 4,
            "plan-reflection": 5,
        }

        current_strength = strength_order.get(current, 2)
        proposed_strength = strength_order.get(proposed, 2)

        # 允许切换的情况：
        # 1. 同级切换（如 plan-execute <-> reflection）
        # 2. 升级（如 react -> plan-execute）
        # 3. 降级但幅度不超过1级（如 reflection -> react，但不允许 reflection -> direct）

        diff = proposed_strength - current_strength

        if diff >= 0:
            # 同级或升级，允许
            return True
        elif diff == -1:
            # 降一级，允许
            return True
        else:
            # 降级幅度过大，不允许
            return False

    def enable(self) -> None:
        """启用智能路由"""
        self.enabled = True
        logger.info("智能路由已启用")

    def disable(self) -> None:
        """禁用智能路由"""
        self.enabled = False
        logger.info("智能路由已禁用")

    def get_stats(self) -> dict:
        """获取路由统计信息"""
        return {
            "total_routes": self._total_routes,
            "successful_routes": self._successful_routes,
            "switches": self._switches,
            "success_rate": (
                self._successful_routes / self._total_routes
                if self._total_routes > 0 else 0
            ),
            "decision_log": self.decision_history.stats(),
        }
