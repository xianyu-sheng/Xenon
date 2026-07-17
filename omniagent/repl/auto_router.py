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
from omniagent.repl.model_pool import ModelPool, FAILURE_THRESHOLD
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
        # P1-B SAAR: 会话感知路由(粘性锁,防止 ReAct/Plan-Execute 中途切模型
        # 致上下文漂移 + prompt cache 失效)
        from omniagent.repl.session_lock import SessionLock
        self.session_lock = SessionLock()
        self.session_lock_enabled = True
        self.drift_threshold = 3  # 连续 N 次决策漂移才释放锁

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

        # P1-B SAAR: 会话粘性锁短路(锁定时优先返回锁定模型,跳过重选)
        locked_ids = self._session_lock_route(user_input, profile, task_tier, count)
        if locked_ids is not None:
            return locked_ids

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

        # P1-B SAAR: 检测到工具调用流时加锁,保证后续请求路由连续(避免中途切模型)
        if (self.session_lock_enabled and result_ids
                and self._is_tool_flow(context_messages, profile)):
            self.session_lock.lock(result_ids[0], task_tier, reason="tool_flow")

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

    # ── P1-B SAAR: 会话感知路由辅助 ──────────────────────────

    def reset_session_lock(self) -> None:
        """显式释放会话锁(新会话 / /reset / clear context 时调用)。"""
        self.session_lock.release()

    def _session_lock_route(
        self, user_input: str, profile: TaskProfile, task_tier: int, count: int,
    ) -> list[str] | None:
        """SAAR 短路:锁有效时返回锁定模型优先列表;None 表示走正常流程。

        释放条件(任一):锁定模型失联/failover 不健康、决策漂移连续超阈值。
        """
        if not self.session_lock_enabled or not self.session_lock.is_locked():
            return None
        locked_id = self.session_lock.locked_model_id
        entry = self._find_entry_by_model_id(locked_id) if locked_id else None
        if not entry or not self._is_healthy(entry):
            # 锁定模型失联或因 failover 不健康 -> 释放,下次 route 重选并重锁
            self.session_lock.release()
            return None
        # 决策漂移检测:任务 tier 与锁定 tier 差距 >=2 级则累计
        if abs(task_tier - self.session_lock.locked_tier) >= 2:
            self.session_lock.drift_count += 1
        else:
            self.session_lock.drift_count = 0
        if self.session_lock.drift_count >= self.drift_threshold:
            self.session_lock.release()
            return None
        # 锁有效:锁定模型优先,补 fallback
        entries = self.pool.select_best(profile, count=count)
        fallback = [e.model_id for e in entries if e.model_id != locked_id]
        result_ids = ([locked_id] + fallback)[:count]
        scores = [self.pool.score_for_profile(e, profile) for e in entries] if entries else []
        self.history.record(RoutingRecord(
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
        ))
        return result_ids

    def _find_entry_by_model_id(self, model_id: str):
        """按 model_id 查找池中条目(pool.get 按 alias,route 结果是 model_id)。"""
        for e in self.pool.list_all():
            if e.model_id == model_id:
                return e
        return None

    @staticmethod
    def _is_healthy(entry) -> bool:
        """SAAR 健康判定:未永久驱逐、断路器未开、连续失败未达阈值。"""
        h = entry.health
        if h.permanently_evicted:
            return False
        if h.circuit_open_until and h.circuit_open_until > time.time():
            return False
        return h.consecutive_failures < FAILURE_THRESHOLD

    @staticmethod
    def _is_tool_flow(context_messages: list[dict] | None, profile: TaskProfile) -> bool:
        """判断是否处于工具调用流(需要工具 + 近期上下文含 tool 角色消息)。

        首次工具任务(尚无 tool 消息)不锁;进入循环后才锁,避免过度粘性。
        """
        if not profile.requires_tools:
            return False
        recent = (context_messages or [])[-4:]
        return any(
            isinstance(m, dict) and m.get("role") in ("tool", "function", "tool_result")
            for m in recent
        )

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
