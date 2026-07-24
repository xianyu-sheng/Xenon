"""v0.4.0 Step 2: ModelPool 测试."""
from __future__ import annotations

from xenon.repl.model_pool import (
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

    def test_first_failure_does_not_open_circuit(self):
        pool = ModelPool()
        pool.register("x/y", alias="y")

        pool.record_failure("y")

        entry = pool.get("y")
        assert entry.health.consecutive_failures == 1
        assert entry.health.circuit_open_until == 0
        assert pool.get_healthy() == [entry]

    def test_expired_cooldown_allows_half_open_probe(self):
        import time

        pool = ModelPool()
        pool.register("x/y", alias="y")
        for _ in range(FAILURE_THRESHOLD):
            pool.record_failure("y")
        pool.get("y").health.circuit_open_until = time.monotonic() - 1

        assert pool.get_healthy() == [pool.get("y")]

    def test_repeated_retry_failures_never_delete_or_permanently_evict(self):
        pool = ModelPool()
        pool.register("x/y", alias="y")
        for _ in range(FAILURE_THRESHOLD):
            pool.record_failure("y")

        for _ in range(10):
            pool.get("y").health.circuit_open_until = 0
            assert pool.record_failure("y", is_retry=True) is False

        entry = pool.get("y")
        assert entry is not None
        assert entry.health.permanently_evicted is False

    def test_circuit_broken_model_not_healthy(self):
        pool = ModelPool()
        pool.register("x/y", alias="y")
        for _ in range(FAILURE_THRESHOLD):
            pool.record_failure("y")
        assert len(pool.get_healthy()) == 0

    def test_selection_never_falls_back_to_open_circuit_models(self):
        pool = ModelPool()
        pool.register("x/y", alias="y")
        for _ in range(FAILURE_THRESHOLD):
            pool.record_failure("y")

        assert pool.select_best(None) == []


class TestModelSelection:
    """模型选择（v0.4.0 Step 10: 层级队列调度）."""

    def test_simple_task_prefers_budget_model(self):
        """简单任务（complexity=0.1）应优先选择低成本模型。"""
        pool = ModelPool()
        pool.register("a/pro", alias="pro", weight=5.0)
        pool.register("b/mini", alias="mini", weight=1.0)

        class Simple:
            complexity = 0.1
            requires_reasoning = False
            requires_code_generation = False
            requires_tools = False
            estimated_tokens = 100
            _tier = 1

        best = pool.select_best(Simple(), count=2)
        assert best[0].alias == "mini"  # tier 2, closest to task tier 1

    def test_complex_task_prefers_flagship_model(self):
        """复杂任务（complexity=0.9）应优先选择旗舰模型。"""
        pool = ModelPool()
        pool.register("a/pro", alias="pro", weight=5.0)
        pool.register("b/mini", alias="mini", weight=1.0)

        class Complex:
            complexity = 0.9
            requires_reasoning = True
            requires_code_generation = True
            requires_tools = True
            estimated_tokens = 8000
            _tier = 5

        best = pool.select_best(Complex(), count=2)
        assert best[0].alias == "pro"  # tier 5, matches task tier 5

    def test_work_stealing_to_higher_tier(self):
        """当 target tier 无模型时，向更高 tier 窃取。"""
        pool = ModelPool()
        pool.register("a/pro", alias="pro", weight=1.0)  # tier 5

        class Medium:
            complexity = 0.5
            _tier = 3

        best = pool.select_best(Medium(), count=2)
        assert len(best) == 1
        assert best[0].alias == "pro"  # stolen from tier 5

    def test_work_stealing_to_lower_tier(self):
        """当 target tier 和更高 tier 都无模型时，向更低 tier 窃取。"""
        pool = ModelPool()
        pool.register("b/mini", alias="mini", weight=1.0)  # tier 2

        class Hard:
            complexity = 0.9
            _tier = 5

        best = pool.select_best(Hard(), count=2)
        assert len(best) == 1
        assert best[0].alias == "mini"  # stolen from tier 2

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
