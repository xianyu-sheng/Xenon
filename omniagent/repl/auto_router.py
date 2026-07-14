"""
v0.4.0: Auto model router.

Integrates DifficultyEstimator + ModelPool to replace
the static get_role_priority() with task-aware model selection.

v0.4.0 Step 9: 添加路由历史记录（RoutingHistory）。
v0.4.0 Step 10: 添加任务 tier 估算，传给 ModelPool 的层级队列。
"""

from __future__ import annotations

import time

from omniagent.repl.difficulty_estimator import DifficultyEstimator, TaskProfile
from omniagent.repl.model_pool import ModelPool
from omniagent.repl.routing_history import RoutingHistory, RoutingRecord


class AutoRouter:
    """Task-aware model router.

    Replaces registry.get_role_priority("planner") throughout the REPL.
    """

    def __init__(
        self,
        model_pool: ModelPool | None = None,
        estimator: DifficultyEstimator | None = None,
        history: RoutingHistory | None = None,
        context_manager: Any = None,
    ):
        self.pool = model_pool or ModelPool()
        self.estimator = estimator or DifficultyEstimator()
        self.history = history or RoutingHistory()
        self.ctx_mgr = context_manager  # v0.5.0：分层上下文管理

    def route(
        self,
        user_input: str,
        context_messages: list[dict] | None = None,
        count: int = 3,
        preferred_models: list[str] | None = None,
    ) -> list[str]:
        """Select best models for the given task.

        Args:
            user_input: The user's input text.
            context_messages: Previous conversation messages.
            count: Number of models to return for fallback.
            preferred_models: v0.5.3: User-specified models (via -m) that
                should always be tried first, before auto-selected models.

        Returns a list of model_ids for fallback (best first).
        """
        profile = self.estimator.estimate(user_input, context_messages or [])

        # Step 10: 估算任务 tier，设置到 profile 上供 ModelPool 层级队列使用
        task_tier = DifficultyEstimator.estimate_tier(profile)
        setattr(profile, "_tier", task_tier)

        # v0.5.0：同步任务 tier 到 ContextManager，用于分层上下文管理
        if self.ctx_mgr is not None:
            self.ctx_mgr.set_active_tier(task_tier)

        entries = self.pool.select_best(profile, count=count)

        result_ids: list[str]
        if entries:
            result_ids = [e.model_id for e in entries]
        else:
            # Fallback: any healthy model
            healthy = self.pool.get_healthy()
            if healthy:
                result_ids = [e.model_id for e in healthy[:count]]
            else:
                # Pool empty: try static registry
                result_ids = self._registry_fallback(count)

        # v0.5.3: 用户显式指定的模型（-m）总是排在最前面
        if preferred_models:
            preferred_set = set(preferred_models)
            # 把 preferred_models 中在结果集里的放到最前面
            prioritized = [m for m in preferred_models if m in result_ids]
            # 追加其他模型（排除已添加的）
            for m in result_ids:
                if m not in prioritized and m not in preferred_set:
                    prioritized.append(m)
            # 如果 preferred_models 全不在结果里，也确保它们排在前面
            for m in preferred_models:
                if m not in prioritized:
                    # 模型可能不在 pool 中（通过 alias 注册的），直接加
                    prioritized.append(m)
            result_ids = prioritized[:count]

        # Step 9: 记录路由决策
        scores = [self.pool.score_for_profile(e, profile) for e in entries] if entries else []
        record = RoutingRecord(
            timestamp=time.time(),
            user_input_preview=user_input[:120],
            intent=profile.intent,
            complexity=profile.complexity,
            requires_reasoning=profile.requires_reasoning,
            requires_code_generation=profile.requires_code_generation,
            requires_tools=profile.requires_tools,
            estimated_tokens=profile.estimated_tokens,
            task_tier=task_tier,
            selected_models=result_ids,
            scores=scores,
        )
        self.history.record(record)

        return result_ids

    def get_active_model_id(self) -> str | None:
        """Return the 'active' model display name for status bar."""
        healthy = self.pool.get_healthy()
        if healthy:
            return healthy[0].model_id
        return None

    def is_empty(self) -> bool:
        """Check if the pool has any registered models."""
        return len(self.pool.list_all()) == 0

    @staticmethod
    def _registry_fallback(count: int) -> list[str]:
        """Fall back to static ModelRegistry if pool is empty."""
        try:
            from omniagent.repl.model_registry import ModelRegistry
            reg = ModelRegistry()
            models = reg.list_models()
            if models:
                return [m.model_id for m in models[:count]]
        except Exception:
            pass
        return []
