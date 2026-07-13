"""v0.4.0 Step 10: Multi-priority queue scheduling tests."""
from __future__ import annotations

import pytest
from omniagent.repl.model_pool import ModelPool, MIN_TIER, MAX_TIER
from omniagent.repl.difficulty_estimator import DifficultyEstimator, TaskProfile


class TestTierAssignment:
    """estimate_tier 的逻辑."""

    def test_greeting_is_tier_1(self):
        profile = TaskProfile(intent="chat", complexity=0.05)
        assert DifficultyEstimator.estimate_tier(profile) == 1

    def test_query_is_tier_2(self):
        profile = TaskProfile(intent="query", complexity=0.3)
        assert DifficultyEstimator.estimate_tier(profile) == 2

    def test_write_code_is_tier_3(self):
        profile = TaskProfile(intent="write_code", complexity=0.45,
                              requires_code_generation=True)
        # complexity 0.45 → tier 3; requires_code_generation 但 c ≤ 0.5，不提升
        assert DifficultyEstimator.estimate_tier(profile) == 3

    def test_refactor_is_tier_4(self):
        profile = TaskProfile(intent="refactor", complexity=0.7,
                              requires_reasoning=True,
                              requires_code_generation=True)
        assert DifficultyEstimator.estimate_tier(profile) == 4

    def test_design_is_tier_5(self):
        profile = TaskProfile(intent="design", complexity=0.9,
                              requires_reasoning=True)
        assert DifficultyEstimator.estimate_tier(profile) == 5

    def test_reasoning_lifts_tier(self):
        profile = TaskProfile(intent="explain", complexity=0.2,
                              requires_reasoning=True)
        # requires_reasoning → at least tier 3
        assert DifficultyEstimator.estimate_tier(profile) == 3


class TestTierQueues:
    """模型注册到 tier 队列."""

    def test_flagship_goes_to_tier_5(self):
        pool = ModelPool()
        pool.register("a/pro")  # _infer_capability → tier 5
        queues = pool.get_tier_queues()
        assert queues[5] == ["pro"]

    def test_budget_goes_to_tier_2(self):
        pool = ModelPool()
        pool.register("b/mini")  # _infer_capability → tier 2
        queues = pool.get_tier_queues()
        assert queues[2] == ["mini"]

    def test_unregister_removes_from_queue(self):
        pool = ModelPool()
        pool.register("a/pro")
        pool.unregister("pro")
        queues = pool.get_tier_queues()
        assert all(len(v) == 0 for v in queues.values())


class TestWorkStealing:
    """工作窃取逻辑."""

    def test_steal_to_higher_tier(self):
        """目标 tier 无模型 → 向更高 tier 窃取."""
        pool = ModelPool()
        pool.register("a/pro")  # tier 5

        class Tier3Task:
            complexity = 0.5
            _tier = 3

        best = pool.select_best(Tier3Task())
        assert len(best) == 1
        assert best[0].alias == "pro"  # stolen from tier 5

    def test_steal_to_lower_tier(self):
        """目标 tier 和更高 tier 都无模型 → 向更低 tier 窃取."""
        pool = ModelPool()
        pool.register("b/mini")  # tier 2

        class Tier5Task:
            complexity = 0.9
            _tier = 5

        best = pool.select_best(Tier5Task())
        assert len(best) == 1
        assert best[0].alias == "mini"  # stolen from tier 2

    def test_empty_pool_all_tiers(self):
        pool = ModelPool()
        assert pool.select_best(TaskProfile()) == []

    def test_circuit_broken_not_stolen(self):
        """断路器打开的模型不被窃取."""
        pool = ModelPool()
        pool.register("a/pro")  # tier 5
        for _ in range(3):
            pool.record_failure("pro")

        # pro is broken → tier 5 queue effectively empty
        class Tier5Task:
            complexity = 0.9
            _tier = 5

        best = pool.select_best(Tier5Task())
        # No healthy models → fallback to all (broken) models
        assert len(best) == 1
        assert best[0].alias == "pro"  # fallback includes all


class TestFromConfig:
    """from_config 重建 tier 队列."""

    def test_roundtrip_restores_queues(self):
        pool = ModelPool()
        pool.register("a/pro", weight=5.0)
        pool.register("b/mini", weight=1.0)

        cfg = pool.to_config()
        pool2 = ModelPool()
        pool2.from_config(cfg)

        queues = pool2.get_tier_queues()
        assert "pro" in queues[5]
        assert "mini" in queues[2]
