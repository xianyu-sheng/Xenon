"""
v0.4.0: Auto model router.

Integrates DifficultyEstimator + ModelPool to replace
the static get_role_priority() with task-aware model selection.
"""

from __future__ import annotations

from omniagent.repl.difficulty_estimator import DifficultyEstimator, TaskProfile
from omniagent.repl.model_pool import ModelPool


class AutoRouter:
    """Task-aware model router.

    Replaces registry.get_role_priority("planner") throughout the REPL.
    """

    def __init__(
        self,
        model_pool: ModelPool | None = None,
        estimator: DifficultyEstimator | None = None,
    ):
        self.pool = model_pool or ModelPool()
        self.estimator = estimator or DifficultyEstimator()

    def route(
        self,
        user_input: str,
        context_messages: list[dict] | None = None,
        count: int = 3,
    ) -> list[str]:
        """Select best models for the given task.

        Returns a list of model_ids for fallback (best first).
        """
        profile = self.estimator.estimate(user_input, context_messages or [])
        entries = self.pool.select_best(profile, count=count)

        if entries:
            return [e.model_id for e in entries]

        # Fallback: any healthy model
        healthy = self.pool.get_healthy()
        if healthy:
            return [e.model_id for e in healthy[:count]]

        # Pool empty: try static registry
        return self._registry_fallback(count)

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
