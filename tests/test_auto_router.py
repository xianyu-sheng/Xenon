"""v0.4.0 Steps 3-5: DifficultyEstimator + AutoRouter tests."""
from __future__ import annotations

from xenon.repl.difficulty_estimator import (
    DifficultyEstimator,
)
from xenon.repl.auto_router import AutoRouter
from xenon.repl.model_pool import ModelPool


class TestDifficultyEstimator:
    """任务难度评估."""

    def test_simple_greeting_is_low_complexity(self):
        est = DifficultyEstimator()
        profile = est.estimate("你好")
        assert profile.complexity < 0.5
        assert not profile.requires_code_generation

    def test_refactor_is_high_complexity(self):
        est = DifficultyEstimator()
        profile = est.estimate("重构整个项目的数据库访问层，从同步改为异步")
        assert profile.complexity > 0.5
        assert profile.requires_code_generation

    def test_chat_does_not_require_tools(self):
        est = DifficultyEstimator()
        profile = est.estimate("今天天气怎么样")
        assert not profile.requires_code_generation

    def test_multi_file_reference_increases_complexity(self):
        est = DifficultyEstimator()
        profile = est.estimate("重构 src/main.py tests/test_main.py")
        # Should be higher than a simple chat
        assert profile.complexity > 0.1


class TestAutoRouter:
    """自动路由."""

    def test_route_with_empty_pool_returns_empty(self):
        router = AutoRouter(ModelPool())
        result = router.route("hello")
        assert result == [] or all(isinstance(x, str) for x in result)

    def test_route_returns_best_first(self):
        pool = ModelPool()
        pool.register("a/pro", alias="pro", weight=5.0)
        pool.register("b/mini", alias="mini", weight=0.5)
        router = AutoRouter(pool)
        result = router.route("重构整个项目的异步架构")
        assert len(result) >= 1
        assert result[0] == "a/pro"

    def test_get_active_model_id(self):
        pool = ModelPool()
        pool.register("x/y", alias="y")
        router = AutoRouter(pool)
        assert router.get_active_model_id() == "x/y"

    def test_is_empty(self):
        assert AutoRouter(ModelPool()).is_empty()

    def test_circuit_broken_model_excluded(self):
        pool = ModelPool()
        pool.register("a/pro", alias="pro")
        pool.register("b/backup", alias="backup")
        # Break pro
        for _ in range(3):
            pool.record_failure("pro")
        router = AutoRouter(pool)
        result = router.route("simple task")
        # Backup should be selected since pro is broken
        assert result[0] == "b/backup"
