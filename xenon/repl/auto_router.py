"""
v0.4.0: Auto model router.

Integrates DifficultyEstimator + ModelPool to replace
the static get_role_priority() with task-aware model selection.

v0.4.0 Step 9: 添加路由历史记录（RoutingHistory）。
v0.4.0 Step 10: 添加任务 tier 估算，传给 ModelPool 的层级队列。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from xenon.repl.difficulty_estimator import DifficultyEstimator, TaskProfile
from xenon.repl.model_pool import ModelPool
from xenon.repl.routing_history import RoutingHistory, RoutingRecord


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
        cache_tracker: Any = None,
    ):
        self.pool = model_pool or ModelPool()
        self.estimator = estimator or DifficultyEstimator()
        self.history = history or RoutingHistory()
        self.ctx_mgr = context_manager  # v0.5.0：分层上下文管理
        self.cache_tracker = cache_tracker
        self.cache_affinity_max_score_gap = 0.25
        self.last_cache_affinity_decision: dict[str, Any] = {
            "applied": False,
            "reason": "not_evaluated",
        }
        # P1-B SAAR: 会话感知路由(粘性锁,防止 ReAct/Plan-Execute 中途切模型
        # 致上下文漂移 + prompt cache 失效)
        from xenon.repl.session_lock import SessionLock
        self.session_lock = SessionLock()
        self.session_lock_enabled = True
        self.drift_threshold = 3  # 连续 N 次决策漂移才释放锁
        # Problem 2: 追踪最近成功的模型，熔断时清空
        self._last_successful_model_id: str | None = None
        # P2-Medium: 并发安全 — 保护 _last_successful_model_id 访问
        self._lock = threading.Lock()

    def route(
        self,
        user_input: str,
        context_messages: list[dict] | None = None,
        count: int = 3,
        preferred_models: list[str] | None = None,
        cache_engine: str | None = None,
        cache_phase: str | None = None,
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
        entries = self._apply_cache_affinity(
            entries,
            profile,
            cache_engine=cache_engine,
            cache_phase=cache_phase,
        )

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
            # Explicit choices may be outside ``select_best(count)``.  Start
            # with them rather than appending them after an already-full
            # fallback list, otherwise slicing would silently discard ``-m``.
            prioritized = list(dict.fromkeys(preferred_models))
            for m in result_ids:
                if m not in prioritized:
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

        # Problem 2: 更新最近成功模型（在路由后，实际成功调用后才会被记录）
        # P2-Medium: 并发安全 — 使用锁保护写入
        if result_ids:
            with self._lock:
                self._last_successful_model_id = result_ids[0]

        return result_ids

    def _apply_cache_affinity(
        self,
        entries: list[Any],
        profile: TaskProfile,
        *,
        cache_engine: str | None = None,
        cache_phase: str | None = None,
    ) -> list[Any]:
        """Use cache warmth only to break a near-tie between equivalent models.

        Capability tier, base routing score, health, explicit preferences and
        the session lock remain authoritative.  In particular, a warm lower
        tier model can never outrank the leading tier through this method.
        """
        tracker = self.cache_tracker
        enabled = bool(tracker and getattr(tracker, "cache_affinity_enabled", False))
        before = [entry.model_id for entry in entries]
        if not enabled:
            self.last_cache_affinity_decision = {
                "applied": False,
                "reason": "disabled" if tracker else "tracker_unavailable",
                "before": before,
                "after": before,
            }
            return entries
        if len(entries) < 2:
            self.last_cache_affinity_decision = {
                "applied": False,
                "reason": "no_peer_candidate",
                "before": before,
                "after": before,
            }
            return entries

        leader = entries[0]
        leader_score = self.pool.score_for_profile(leader, profile)
        peer_indexes: list[int] = []
        evidence: dict[str, dict[str, Any]] = {}
        for index, entry in enumerate(entries):
            base_score = self.pool.score_for_profile(entry, profile)
            if entry.capability.tier != leader.capability.tier:
                continue
            if leader_score - base_score > self.cache_affinity_max_score_gap:
                continue
            try:
                affinity = tracker.model_cache_affinity(entry.model_id)
            except Exception:
                affinity = {"eligible": False, "score": 0.0, "reason": "tracker_error"}
            lane_evidence: dict[str, Any] = {}
            lanes = getattr(self.ctx_mgr, "prompt_lanes", None)
            if lanes is not None and cache_engine and cache_phase:
                try:
                    lane_evidence = lanes.model_warmth(
                        entry.model_id,
                        engine=cache_engine,
                        phase=cache_phase,
                    )
                except Exception:
                    lane_evidence = {
                        "eligible": False,
                        "score": 0.0,
                        "reason": "lane_tracker_error",
                    }
            if lane_evidence.get("eligible"):
                if affinity.get("eligible"):
                    affinity = {
                        **affinity,
                        "score": min(
                            1.0,
                            float(affinity.get("score", 0.0))
                            + float(lane_evidence.get("score", 0.0)),
                        ),
                        "lane": lane_evidence,
                    }
                else:
                    affinity = {
                        **lane_evidence,
                        "provider_reason": affinity.get("reason"),
                        "evidence": "local_exact_prefix",
                    }
            elif lane_evidence:
                affinity = {**affinity, "lane": lane_evidence}
            peer_indexes.append(index)
            evidence[entry.model_id] = {
                **affinity,
                "base_score": round(base_score, 4),
                "tier": entry.capability.tier,
            }

        warm_peers = [
            entries[index] for index in peer_indexes
            if evidence[entries[index].model_id].get("eligible")
        ]
        if not warm_peers:
            self.last_cache_affinity_decision = {
                "applied": False,
                "reason": "no_warm_equivalent_peer",
                "before": before,
                "after": before,
                "evidence": evidence,
            }
            return entries

        peers = [entries[index] for index in peer_indexes]
        peers.sort(
            key=lambda entry: (
                float(evidence[entry.model_id].get("score", 0.0)),
                float(evidence[entry.model_id]["base_score"]),
            ),
            reverse=True,
        )
        reordered = list(entries)
        for index, entry in zip(peer_indexes, peers):
            reordered[index] = entry
        after = [entry.model_id for entry in reordered]
        self.last_cache_affinity_decision = {
            "applied": after != before,
            "reason": "warm_equivalent_peer" if after != before else "leader_already_preferred",
            "before": before,
            "after": after,
            "score_gap_limit": self.cache_affinity_max_score_gap,
            "evidence": evidence,
        }
        return reordered

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
        # Problem 2: 清空最近成功模型记录，保持状态一致性
        with self._lock:
            self._last_successful_model_id = None

    def record_model_success(self, model_id: str) -> None:
        """记录模型调用成功（更新 _last_successful_model_id）。

        Problem 2: 在引擎层调用成功后调用此方法，用于跟踪最近成功的模型。
        """
        with self._lock:
            self._last_successful_model_id = model_id

    def _session_lock_route(
        self, user_input: str, profile: TaskProfile, task_tier: int, count: int,
    ) -> list[str] | None:
        """SAAR 短路:锁有效时返回锁定模型优先列表;None 表示走正常流程。

        释放条件(任一):锁定模型失联/failover 不健康、决策漂移连续超阈值。
        Problem 2: 熔断时清空 _last_successful_model_id。
        """
        if not self.session_lock_enabled or not self.session_lock.is_locked():
            return None
        locked_id = self.session_lock.locked_model_id
        entry = self._find_entry_by_model_id(locked_id) if locked_id else None
        if not entry or not self._is_healthy(entry):
            # 锁定模型失联或因 failover 不健康 -> 释放,下次 route 重选并重锁
            self.session_lock.release()
            # Problem 2: 熔断时清空最近成功模型记录
            with self._lock:
                self._last_successful_model_id = None
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
        self.last_cache_affinity_decision = {
            "applied": False,
            "reason": "session_lock_precedence",
            "before": result_ids,
            "after": result_ids,
        }
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
        if h.circuit_open_until and h.circuit_open_until > time.monotonic():
            return False
        # Cooldown expiry is the half-open probe: the model must be eligible
        # for one call so that a success can reset consecutive_failures.
        return True

    @staticmethod
    def _is_tool_flow(context_messages: list[dict] | None, profile: TaskProfile) -> bool:
        """判断是否处于工具调用流(需要工具 + 近期上下文含 tool 角色消息)。

        首次工具任务(尚无 tool 消息)不锁;进入循环后才锁,避免过度粘性。
        """
        if not profile.requires_tools:
            return False
        recent = (context_messages or [])[-4:]
        return any(
            isinstance(m, dict)
            and (
                m.get("role") in ("tool", "function", "tool_result")
                or "[工具调用:" in str(m.get("content", ""))
                or "[工具结果:" in str(m.get("content", ""))
            )
            for m in recent
        )

    @staticmethod
    def _registry_fallback(count: int) -> list[str]:
        """Fall back to static ModelRegistry if pool is empty."""
        try:
            from xenon.repl.model_registry import ModelRegistry
            reg = ModelRegistry()
            models = reg.list_models()
            if models:
                return [m.model_id for m in models[:count]]
        except Exception:
            pass
        return []
