"""P1-B: SAAR 会话感知路由(粘性锁)测试。"""
from __future__ import annotations

import time

import pytest

from xenon.repl.auto_router import AutoRouter
from xenon.repl.difficulty_estimator import DifficultyEstimator, TaskProfile
from xenon.repl.model_pool import ModelPool, FAILURE_THRESHOLD
from xenon.repl.session_lock import SessionLock


class _FakeEstimator:
    """可控 estimator:返回固定 TaskProfile(estimate_tier 走真实静态方法)。"""
    def __init__(self, profile: TaskProfile):
        self._p = profile

    def estimate(self, user_input, context_messages=None):
        return self._p


class TestSessionLock:
    """纯状态机。"""

    def test_lock_and_is_locked(self):
        sl = SessionLock()
        assert not sl.is_locked()
        sl.lock("a/pro", 3, "tool_flow")
        assert sl.is_locked()
        assert sl.locked_model_id == "a/pro"
        assert sl.locked_tier == 3
        assert sl.drift_count == 0

    def test_release_clears(self):
        sl = SessionLock()
        sl.lock("a/pro", 3)
        sl.release()
        assert not sl.is_locked()
        assert sl.locked_model_id is None
        assert sl.locked_tier == 0


class TestSAARLockAcquisition:
    """锁获取:仅在有工具流时锁定。"""

    def test_lock_on_tool_flow(self):
        pool = ModelPool()
        pool.register("a/pro", alias="pro", weight=5.0)
        router = AutoRouter(pool)
        router.estimator = _FakeEstimator(TaskProfile(requires_tools=True, complexity=0.5))
        ctx = [{"role": "user", "content": "x"}, {"role": "tool", "content": "result"}]
        router.route("do something", context_messages=ctx)
        assert router.session_lock.is_locked()
        assert router.session_lock.locked_model_id == "a/pro"

    def test_no_lock_without_tool_messages(self):
        """requires_tools=True 但上下文无 tool 消息(首次工具任务)-> 不锁。"""
        pool = ModelPool()
        pool.register("a/pro", alias="pro", weight=5.0)
        router = AutoRouter(pool)
        router.estimator = _FakeEstimator(TaskProfile(requires_tools=True, complexity=0.5))
        router.route("do something", context_messages=None)
        assert not router.session_lock.is_locked()

    def test_no_lock_when_not_requires_tools(self):
        """非工具任务(即使上下文有 tool 消息)-> 不锁。"""
        pool = ModelPool()
        pool.register("a/pro", alias="pro", weight=5.0)
        router = AutoRouter(pool)
        router.estimator = _FakeEstimator(TaskProfile(requires_tools=False, complexity=0.5))
        ctx = [{"role": "tool", "content": "r"}]
        router.route("chat", context_messages=ctx)
        assert not router.session_lock.is_locked()


class TestSAARShortCircuit:
    """锁有效时 route 短路返回锁定模型优先。"""

    def test_locked_model_returned_first(self):
        pool = ModelPool()
        pool.register("a/pro", alias="pro", weight=5.0)
        pool.register("b/mini", alias="mini", weight=0.5)
        router = AutoRouter(pool)
        router.estimator = _FakeEstimator(TaskProfile(requires_tools=True, complexity=0.5))
        ctx = [{"role": "tool", "content": "r"}]
        router.route("x", context_messages=ctx)  # 加锁
        locked = router.session_lock.locked_model_id
        assert locked == "a/pro"
        # 第二次 route(锁有效):锁定模型排首位
        second = router.route("y", context_messages=ctx)
        assert second[0] == locked

    def test_release_on_unhealthy(self):
        """锁定模型断路器打开(failover 后)-> 释放,走正常流程。"""
        pool = ModelPool()
        pool.register("a/pro", alias="pro", weight=5.0)
        router = AutoRouter(pool)
        router.session_lock.lock("a/pro", 3, "tool_flow")
        pool.get("pro").health.circuit_open_until = time.time() + 60  # 断路器开
        profile = TaskProfile(requires_tools=True, complexity=0.5)
        result = router._session_lock_route("x", profile, 3, 3)
        assert result is None  # 释放,交正常流程
        assert not router.session_lock.is_locked()

    def test_release_on_consecutive_failures(self):
        """连续失败达阈值 -> 视为不健康释放。"""
        pool = ModelPool()
        pool.register("a/pro", alias="pro", weight=5.0)
        router = AutoRouter(pool)
        router.session_lock.lock("a/pro", 3, "tool_flow")
        pool.get("pro").health.consecutive_failures = FAILURE_THRESHOLD
        profile = TaskProfile(requires_tools=True, complexity=0.5)
        assert router._session_lock_route("x", profile, 3, 3) is None
        assert not router.session_lock.is_locked()

    def test_release_on_drift(self):
        """决策漂移(task_tier 与锁定 tier 差距>=2)连续超阈值 -> 释放。"""
        pool = ModelPool()
        pool.register("a/pro", alias="pro", weight=5.0)
        router = AutoRouter(pool)
        router.drift_threshold = 2
        router.session_lock.lock("a/pro", 3, "tool_flow")  # locked_tier=3
        profile_tier5 = TaskProfile(requires_tools=True, complexity=0.9)  # estimate_tier -> 5
        # 第一次漂移:drift_count=1 < 2,仍锁
        router._session_lock_route("x", profile_tier5, 5, 3)
        assert router.session_lock.is_locked()
        # 第二次漂移:drift_count=2 >= 2,释放
        router._session_lock_route("x", profile_tier5, 5, 3)
        assert not router.session_lock.is_locked()

    def test_no_drift_within_threshold(self):
        """tier 差距 <2 不计漂移。"""
        pool = ModelPool()
        pool.register("a/pro", alias="pro", weight=5.0)
        router = AutoRouter(pool)
        router.drift_threshold = 2
        router.session_lock.lock("a/pro", 3, "tool_flow")
        profile = TaskProfile(requires_tools=True, complexity=0.5)  # tier 3
        for _ in range(5):
            router._session_lock_route("x", profile, 3, 3)
        assert router.session_lock.is_locked()  # 无漂移,保持锁定

    def test_reset_session_lock(self):
        pool = ModelPool()
        pool.register("a/pro", alias="pro", weight=5.0)
        router = AutoRouter(pool)
        router.session_lock.lock("a/pro", 3, "tool_flow")
        router.reset_session_lock()
        assert not router.session_lock.is_locked()
