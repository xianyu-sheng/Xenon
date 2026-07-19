"""P2: 性能偏好(perf_profile) + 资源感知(in_flight) + 限流退避(retry) 测试。

覆盖:
- ModelPool.perf_profile 默认值 / set_perf_profile 校验
- _score 在 fast|cost|balanced 三种偏好下的相对排序
- in_flight 负载因子(fast 偏好下惩罚放大)
- acquire / release 并发计数(含未知模型 no-op、release 下限 0)
- _is_transient_error / _extract_retry_after 静态辅助
- _call_llm 全链 429 退避重试 / 400 终端错误不重试
"""
import httpx
import pytest

from xenon.repl.model_pool import ModelPool
from xenon.repl.difficulty_estimator import TaskProfile


def _http_error(status: int, headers=None) -> httpx.HTTPStatusError:
    """构造一个 HTTPStatusError,用于退避辅助测试。"""
    req = httpx.Request("POST", "http://x")
    resp = httpx.Response(status, request=req, headers=headers or {})
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


# ── perf_profile / _score ───────────────────────────────

class TestPerfProfile:
    def test_default_balanced(self):
        assert ModelPool().perf_profile == "balanced"

    @pytest.mark.parametrize("p", ["fast", "cost", "balanced"])
    def test_set_valid_profiles(self, p):
        pool = ModelPool()
        assert pool.set_perf_profile(p) is True
        assert pool.perf_profile == p

    def test_set_invalid_returns_false_and_unchanged(self):
        pool = ModelPool()
        assert pool.set_perf_profile("turbo") is False
        assert pool.perf_profile == "balanced"

    def test_fast_prefers_low_tier(self):
        """fast 降低 quality 权重 + tier 惩罚 -> budget 模型胜出。"""
        pool = ModelPool()
        e_mini = pool.register("deepseek/deepseek-v4-flash", alias="mini")  # tier 2
        e_pro = pool.register("openai/gpt-4o", alias="pro")  # tier 5
        pool.set_perf_profile("fast")
        profile = TaskProfile(requires_tools=True, complexity=0.5)
        assert pool._score(e_mini, profile) > pool._score(e_pro, profile)

    def test_balanced_prefers_flagship(self):
        """balanced quality 权重最高 -> flagship 胜出(与 fast 形成对比)。"""
        pool = ModelPool()
        e_mini = pool.register("deepseek/deepseek-v4-flash", alias="mini")
        e_pro = pool.register("openai/gpt-4o", alias="pro")
        profile = TaskProfile(requires_tools=True, complexity=0.5)
        assert pool._score(e_pro, profile) > pool._score(e_mini, profile)

    def test_cost_prefers_cheap(self):
        """cost 提升 cost_efficiency 权重 -> 高性价比 budget 模型胜出。"""
        pool = ModelPool()
        e_mini = pool.register("deepseek/deepseek-v4-flash", alias="mini")  # cost_eff 0.7
        e_pro = pool.register("openai/gpt-4o", alias="pro")  # cost_eff 0.1
        pool.set_perf_profile("cost")
        profile = TaskProfile(complexity=0.5)
        assert pool._score(e_mini, profile) > pool._score(e_pro, profile)


# ── in_flight 负载因子 ─────────────────────────────────

class TestInFlightPenalty:
    def test_in_flight_lowers_score(self):
        pool = ModelPool()
        e_a = pool.register("openai/gpt-4o", alias="a")
        e_b = pool.register("openai/gpt-4o", alias="b")  # 同 model_id 同能力
        e_a.health.in_flight = 3
        e_b.health.in_flight = 0
        profile = TaskProfile(complexity=0.5)
        assert pool._score(e_b, profile) > pool._score(e_a, profile)

    def test_fast_amplifies_load_penalty(self):
        """fast 偏好下 in_flight 惩罚系数 1.5 > balanced 的 1.0。

        weight 取大值避免 _score 的 max(.,0) 截断掩盖差异。
        """
        pool = ModelPool()
        e = pool.register("deepseek/deepseek-v4-flash", alias="a", weight=10.0)
        profile = TaskProfile(complexity=0.5)

        e.health.in_flight = 0
        s_idle_b = pool._score(e, profile)
        e.health.in_flight = 2
        s_loaded_b = pool._score(e, profile)
        drop_balanced = s_idle_b - s_loaded_b

        pool.set_perf_profile("fast")
        e.health.in_flight = 0
        s_idle_f = pool._score(e, profile)
        e.health.in_flight = 2
        s_loaded_f = pool._score(e, profile)
        drop_fast = s_idle_f - s_loaded_f

        assert drop_fast > drop_balanced


# ── acquire / release ───────────────────────────────────

class TestAcquireRelease:
    def test_acquire_increments(self):
        pool = ModelPool()
        pool.register("openai/gpt-4o", alias="pro")
        assert pool.get("pro").health.in_flight == 0
        pool.acquire("openai/gpt-4o")
        pool.acquire("openai/gpt-4o")
        assert pool.get("pro").health.in_flight == 2

    def test_release_decrements(self):
        pool = ModelPool()
        pool.register("openai/gpt-4o", alias="pro")
        pool.acquire("openai/gpt-4o")
        pool.acquire("openai/gpt-4o")
        pool.release("openai/gpt-4o")
        assert pool.get("pro").health.in_flight == 1

    def test_release_floor_zero(self):
        """未 acquire 就 release 不应变为负数。"""
        pool = ModelPool()
        pool.register("openai/gpt-4o", alias="pro")
        pool.release("openai/gpt-4o")
        pool.release("openai/gpt-4o")
        assert pool.get("pro").health.in_flight == 0

    def test_acquire_unknown_model_noop(self):
        pool = ModelPool()
        pool.acquire("nobody/unknown")  # 不存在,不抛错


# ── 限流退避辅助 ───────────────────────────────────────

class TestTransientError:
    def test_429_transient(self):
        from xenon.engine.plan_execute_engine import PlanExecuteEngine
        assert PlanExecuteEngine._is_transient_error(_http_error(429)) is True

    def test_503_transient(self):
        from xenon.engine.plan_execute_engine import PlanExecuteEngine
        assert PlanExecuteEngine._is_transient_error(_http_error(503)) is True

    def test_400_not_transient(self):
        from xenon.engine.plan_execute_engine import PlanExecuteEngine
        assert PlanExecuteEngine._is_transient_error(_http_error(400)) is False

    def test_network_error_transient(self):
        from xenon.engine.plan_execute_engine import PlanExecuteEngine
        req = httpx.Request("POST", "http://x")
        assert PlanExecuteEngine._is_transient_error(
            httpx.ConnectError("boom", request=req)) is True

    def test_none_not_transient(self):
        from xenon.engine.plan_execute_engine import PlanExecuteEngine
        assert PlanExecuteEngine._is_transient_error(None) is False


class TestExtractRetryAfter:
    def test_header_value(self):
        from xenon.engine.plan_execute_engine import PlanExecuteEngine
        err = _http_error(429, headers={"retry-after": "5"})
        assert PlanExecuteEngine._extract_retry_after(err, default=2.0) == 5.0

    def test_default_when_absent(self):
        from xenon.engine.plan_execute_engine import PlanExecuteEngine
        assert PlanExecuteEngine._extract_retry_after(_http_error(429), default=2.0) == 2.0

    def test_capped_at_30(self):
        from xenon.engine.plan_execute_engine import PlanExecuteEngine
        err = _http_error(429, headers={"retry-after": "60"})
        assert PlanExecuteEngine._extract_retry_after(err, default=2.0) == 30.0

    def test_non_http_returns_default(self):
        from xenon.engine.plan_execute_engine import PlanExecuteEngine
        req = httpx.Request("POST", "http://x")
        err = httpx.ConnectError("boom", request=req)
        assert PlanExecuteEngine._extract_retry_after(err, default=3.0) == 3.0


# ── _call_llm 限流退避端到端 ────────────────────────────

class TestCallLLMRetry:
    def test_retries_on_429_then_succeeds(self, monkeypatch):
        import xenon.engine.base as base
        from xenon.engine.plan_execute_engine import PlanExecuteEngine
        from xenon.engine.callbacks import EngineCallback

        calls = {"n": 0}
        req = httpx.Request("POST", "http://x")

        def fake_chat(model_id, messages, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                resp = httpx.Response(429, request=req, headers={"retry-after": "0"})
                raise httpx.HTTPStatusError("429", request=req, response=resp)
            return "ok"

        monkeypatch.setattr(base, "chat_completion", fake_chat)
        eng = PlanExecuteEngine(["m1"], callback=EngineCallback())
        result = eng._call_llm([{"role": "user", "content": "hi"}], 100)
        assert result == "ok"
        assert calls["n"] == 2  # 第一次 429 退避后重试成功

    def test_no_retry_on_400(self, monkeypatch):
        """400 是终端错误,立即上抛,不触发退避重试。"""
        import xenon.engine.base as base
        from xenon.engine.plan_execute_engine import PlanExecuteEngine
        from xenon.engine.callbacks import EngineCallback

        req = httpx.Request("POST", "http://x")

        def fake_chat(model_id, messages, **kw):
            resp = httpx.Response(400, request=req)
            raise httpx.HTTPStatusError("400", request=req, response=resp)

        monkeypatch.setattr(base, "chat_completion", fake_chat)
        eng = PlanExecuteEngine(["m1"], callback=EngineCallback())
        with pytest.raises(RuntimeError, match="请求被拒"):
            eng._call_llm([{"role": "user", "content": "hi"}], 100)
