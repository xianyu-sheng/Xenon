"""v0.4.0 Step 2: ModelPool 测试."""
from __future__ import annotations

import pytest
from omniagent.repl.model_pool import (
    ModelPool, _infer_capability, FAILURE_THRESHOLD,
)


class TestCapabilityInference:
    """模型能力推断."""

    def test_flagship_tier(self):
        cap = _infer_capability("deepseek/deepseek-v4-pro")
        assert cap.tier == 5

    def test_budget_tier(self):
        cap = _infer_capability("deepseek/deepseek-v4-flash")
        assert cap.tier == 2

    def test_coding_score_high_for_coder_models(self):
        cap = _infer_capability("deepseek/deepseek-v4-pro")
        assert cap.coding_score > 0.7

    def test_cost_efficiency_budget_higher_than_flagship(self):
        cheap = _infer_capability("deepseek/deepseek-v4-flash")
        expensive = _infer_capability("openai/gpt-4o")
        assert cheap.cost_efficiency > expensive.cost_efficiency


class TestModelPoolRegistration:
    """模型池注册/注销."""

    def test_register_and_list(self):
        pool = ModelPool()
        pool.register("deepseek/deepseek-v4-pro", weight=2.0)
        entries = pool.list_all()
        assert len(entries) == 1
        assert entries[0].model_id == "deepseek/deepseek-v4-pro"
        assert entries[0].weight == 2.0

    def test_unregister_returns_bool(self):
        pool = ModelPool()
        pool.register("x/y", alias="y")
        assert pool.unregister("y") is True
        assert pool.unregister("y") is False

    def test_duplicate_alias_overwrites(self):
        pool = ModelPool()
        pool.register("a/b", alias="b", weight=1.0)
        pool.register("a/c", alias="b", weight=3.0)
        assert len(pool.list_all()) == 1
        assert pool.get("b").weight == 3.0


class TestHealthTracking:
    """健康追踪."""

    def test_success_resets_failures(self):
        pool = ModelPool()
        pool.register("x/y", alias="y")
        pool.record_failure("y")
        pool.record_failure("y")
        pool.record_success("y", latency=1.5)
        e = pool.get("y")
        assert e.health.consecutive_failures == 0
        assert e.health.avg_latency == 1.5

    def test_circuit_breaker_opens_after_threshold(self):
        pool = ModelPool()
        pool.register("x/y", alias="y")
        for _ in range(FAILURE_THRESHOLD):
            pool.record_failure("y")
        e = pool.get("y")
        assert e.health.circuit_open_until > 0

    def test_circuit_broken_model_not_healthy(self):
        pool = ModelPool()
        pool.register("x/y", alias="y")
        for _ in range(FAILURE_THRESHOLD):
            pool.record_failure("y")
        assert len(pool.get_healthy()) == 0


class TestModelSelection:
    """模型选择."""

    def test_select_returns_higher_weight_first(self):
        pool = ModelPool()
        pool.register("a/pro", alias="pro", weight=5.0)
        pool.register("b/mini", alias="mini", weight=1.0)

        class Simple:
            complexity = 0.1
            requires_reasoning = False
            requires_code_generation = False
            requires_tools = False
            estimated_tokens = 100

        best = pool.select_best(Simple(), count=2)
        assert best[0].alias == "pro"

    def test_empty_pool(self):
        assert ModelPool().select_best(None) == []

    def test_serialization_roundtrip(self):
        pool = ModelPool()
        pool.register("a/x", alias="x", weight=2.0, api_key="sk-test")
        pool.register("b/y", alias="y", weight=0.5)
        cfg = pool.to_config()
        pool2 = ModelPool()
        pool2.from_config(cfg)
        assert len(pool2.list_all()) == 2
        assert pool2.get("x").weight == 2.0
        assert pool2.get("x").api_key == "sk-test"
