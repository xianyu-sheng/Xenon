"""
智能路由器 - Xenon 推理范式自动选择

根据用户输入和上下文自动选择最合适的推理范式。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from xenon.repl.feature_extractor import FeatureExtractor
from xenon.repl.paradigm_selector import ParadigmSelector, RoutingDecision

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

            # Step 3: 判断是否需要切换
            if decision.paradigm == current_mode:
                # 与当前模式一致，无需切换
                logger.debug(f"推荐范式与当前一致: {current_mode}")
                return None

            # Step 4: 置信度检查
            if decision.confidence < self.confidence_threshold:
                logger.info(
                    f"置信度 {decision.confidence:.2f} 低于阈值 "
                    f"{self.confidence_threshold}，不切换"
                )
                return None

            # Step 5: 优先级检查（避免从强范式降级到弱范式）
            if self.switch_only_on_improvement:
                if not self._is_improvement(current_mode, decision.paradigm):
                    logger.info(
                        f"不建议从 {current_mode} 切换到 {decision.paradigm}，"
                        f"保持当前范式"
                    )
                    return None

            # 通过所有检查，建议切换
            self._successful_routes += 1
            self._switches += 1
            return decision

        except Exception as e:
            logger.error(f"路由决策失败: {e}", exc_info=True)
            return None

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
        }
