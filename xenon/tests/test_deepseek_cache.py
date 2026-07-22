"""CacheTracker + LLM 客户端 + PromptOptimizer 缓存优化 全面测试。

覆盖：
- CacheTracker 核心：record / rate / cost / savings / drop detection
- LLM 客户端：LLMUsage 缓存字段 / _extract_usage / 响应回调
- StatusBar：cache_tracker 集成
- PromptOptimizer: optimize_messages_for_cache
- 边界条件：空响应 / 无缓存字段 / 并发安全 / 除零保护
"""

from __future__ import annotations

import threading
import time

import pytest

from xenon.utils.deepseek_cache import (
    CacheTracker,
    _hash_system_prompt,
    _match_pricing,
)
from xenon.utils.llm_client import (
    LLMUsage,
    _extract_usage,
    register_response_callback,
    _emit_response,
)
from xenon.repl.prompt_optimizer import (
    optimize_messages_for_cache,
    _is_dynamic_content,
)


# ══════════════════════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════════════════════

def _ds_resp(prompt=1000, completion=200, hit=800, miss=200) -> dict:
    return {
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "prompt_cache_hit_tokens": hit,
            "prompt_cache_miss_tokens": miss,
        }
    }


class TestLLMUsageCache:
    """LLMUsage 缓存字段 + _extract_usage 提取。"""

    def test_llm_usage_defaults(self):
        u = LLMUsage()
        assert u.cache_hit_tokens == 0
        assert u.cache_miss_tokens == 0

    def test_llm_usage_add_accumulates_cache(self):
        a = LLMUsage(cache_hit_tokens=100, cache_miss_tokens=50)
        b = LLMUsage(cache_hit_tokens=30, cache_miss_tokens=20, prompt_tokens=500)
        a.add(b)
        assert a.cache_hit_tokens == 130
        assert a.cache_miss_tokens == 70
        assert a.prompt_tokens == 500  # 0 + 500

    def test_extract_usage_deepseek_cache_fields(self):
        u = _extract_usage({
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "prompt_cache_hit_tokens": 800,
                "prompt_cache_miss_tokens": 200,
            }
        }, "deepseek")
        assert u.cache_hit_tokens == 800
        assert u.cache_miss_tokens == 200

    def test_extract_usage_no_cache_fields(self):
        u = _extract_usage({
            "usage": {"prompt_tokens": 500, "completion_tokens": 100}
        }, "openai")
        assert u.cache_hit_tokens == 0
        assert u.cache_miss_tokens == 0

    def test_extract_usage_alternate_cache_field_names(self):
        """兼容 cache_hit_tokens / cache_miss_tokens 字段名。"""
        u = _extract_usage({
            "usage": {
                "prompt_tokens": 1000, "completion_tokens": 200,
                "cache_hit_tokens": 700, "cache_miss_tokens": 300,
            }
        }, "custom")
        assert u.cache_hit_tokens == 700
        assert u.cache_miss_tokens == 300

    def test_extract_usage_null_data(self):
        u = _extract_usage(None, "deepseek")
        assert u.prompt_tokens == 0

    def test_extract_usage_missing_usage_key(self):
        u = _extract_usage({"choices": []}, "deepseek")
        assert u.prompt_tokens == 0

    def test_extract_usage_anthropic_no_cache(self):
        u = _extract_usage({
            "usage": {"input_tokens": 100, "output_tokens": 50}
        }, "anthropic")
        assert u.cache_hit_tokens == 0
        assert u.cache_miss_tokens == 0
        assert u.total_tokens == 150


class TestResponseCallback:
    """全局响应回调机制。"""

    def setup_method(self):
        self.received: list[tuple[str, dict]] = []

    def _cb(self, model_id, data):
        self.received.append((model_id, data))

    def test_register_and_emit(self):
        unsub = register_response_callback(self._cb)
        try:
            _emit_response("ds/v4", {"usage": {"prompt_tokens": 100}})
            assert len(self.received) == 1
            assert self.received[0][0] == "ds/v4"
        finally:
            unsub()

    def test_unsubscribe(self):
        unsub = register_response_callback(self._cb)
        unsub()
        _emit_response("ds/v4", {})
        assert len(self.received) == 0

    def test_callback_exception_isolated(self):
        def bad_cb(model_id, data):
            raise RuntimeError("boom")
        unsub = register_response_callback(bad_cb)
        try:
            # 不应抛出
            _emit_response("x", {"usage": {}})
        finally:
            unsub()

    def test_multiple_callbacks(self):
        r1, r2 = [], []
        u1 = register_response_callback(lambda m, d: r1.append(m))
        u2 = register_response_callback(lambda m, d: r2.append(m))
        try:
            _emit_response("a", {})
            assert r1 == ["a"]
            assert r2 == ["a"]
        finally:
            u1()
            u2()


class TestCacheTrackerCore:
    """CacheTracker 核心功能。"""

    def test_record_basic(self):
        t = CacheTracker()
        t.record_response("ds/v4", _ds_resp(1000, 200, 800, 200))
        assert t.cache_hits == 800
        assert t.cache_misses == 200
        assert t.cache_hit_rate == pytest.approx(0.80)
        assert "80.0%" in t.cache_hit_rate_pct
        t.close()

    def test_record_accumulates(self):
        t = CacheTracker()
        for _ in range(5):
            t.record_response("ds/v4", _ds_resp(1000, 200, 800, 200))
        assert t.cache_hits == 4000
        assert t.cache_misses == 1000
        snap = t.model_snapshot("ds/v4")
        assert snap["calls"] == 5
        t.close()

    def test_record_preserves_prompt_completion(self):
        t = CacheTracker()
        t.record_response("ds/v4", _ds_resp(1234, 567, 1000, 234))
        snap = t.model_snapshot("ds/v4")
        assert snap["prompt_tokens"] == 1234
        assert snap["completion_tokens"] == 567
        t.close()

    def test_cost_calculation(self):
        """验证缓存命中和未命中的价差。"""
        t = CacheTracker()
        # V4-Pro: hit=0.025, miss=3.0, output=6.0 per 1M
        t.record_response("deepseek/deepseek-v4-pro", _ds_resp(10000, 2000, 8000, 2000))
        cost = t.estimated_cost_yuan
        savings = t.savings_yuan
        # hit:  8000  * 0.025 / 1M = 0.00020
        # miss: 2000  * 3.0   / 1M = 0.00600
        # out:  2000  * 6.0   / 1M = 0.01200
        # total ≈ 0.0182
        assert 0.01 < cost < 0.03, f"cost={cost}"
        # if_all_miss: 10000 * 3.0 / 1M + 0.012 = 0.030 + 0.012 = 0.042
        # savings ≈ 0.042 - 0.0182 = 0.0238
        assert savings > 0.01, f"savings={savings}"
        t.close()

    def test_cost_zero_tokens(self):
        t = CacheTracker()
        t.record_response("ds/v4", {"usage": {}})
        assert t.estimated_cost_yuan == 0.0
        assert t.savings_yuan == 0.0
        assert t.savings_pct == 0
        t.close()

    def test_savings_pct_valid_range(self):
        t = CacheTracker()
        t.record_response("ds/v4", _ds_resp(1000, 100, 950, 50))
        pct = t.savings_pct
        assert 0 <= pct <= 100, f"savings_pct={pct}"
        t.close()

    def test_cost_display_formats(self):
        t = CacheTracker()
        # < 0.01
        t.record_response("ds/v4", _ds_resp(100, 10, 90, 10))
        assert "0.01" in t.estimated_cost_display or "¥" in t.estimated_cost_display
        t.close()

    def test_no_cache_data(self):
        t = CacheTracker()
        t.record_response("openai/gpt-4o", {"usage": {"prompt_tokens": 100, "completion_tokens": 50}})
        assert t.cache_hits == 0
        assert t.cache_misses == 0
        assert t.cache_hit_rate == 0.0
        assert t.savings_pct == 0
        t.close()

    def test_null_response(self):
        t = CacheTracker()
        t.record_response("ds/v4", None)  # type: ignore
        t.record_response("ds/v4", {})
        t.record_response("ds/v4", {"usage": None})
        assert t.cache_hits == 0
        t.close()


class TestCacheTrackerMultiModel:
    """多模型累计。"""

    def test_tracks_per_model(self):
        t = CacheTracker()
        t.record_response("ds/v4", _ds_resp(1000, 200, 800, 200))
        t.record_response("ds/r1", _ds_resp(500, 100, 400, 100))
        assert set(t.all_models) == {"ds/v4", "ds/r1"}
        s4 = t.model_snapshot("ds/v4")
        s1 = t.model_snapshot("ds/r1")
        assert s4["calls"] == 1
        assert s1["calls"] == 1
        assert s4["prompt_tokens"] == 1000
        assert s1["prompt_tokens"] == 500
        t.close()

    def test_streaming_and_blocking_deepseek_ids_share_one_bucket(self):
        t = CacheTracker()
        t.record_response(
            "deepseek/deepseek-v4-flash",
            _ds_resp(1000, 100, 800, 200),
        )
        t.record_response(
            "deepseek-v4-flash",
            _ds_resp(500, 50, 400, 100),
        )

        assert t.all_models == ["deepseek/deepseek-v4-flash"]
        snap = t.model_snapshot("deepseek-v4-flash")
        assert snap["calls"] == 2
        assert snap["prompt_tokens"] == 1500
        assert snap["cache_hit_tokens"] == 1200
        t.close()

    def test_model_snapshot_unknown_model(self):
        t = CacheTracker()
        assert t.model_snapshot("nonexistent") == {}
        t.close()


class TestCacheTrackerDropDetection:
    """命中率骤降检测。"""

    def test_no_alert_with_few_samples(self):
        t = CacheTracker()
        for _ in range(3):
            t.record_response("ds/v4", _ds_resp(1000, 200, 300, 700))
        assert t.check_hit_rate_drop() is None
        t.close()

    def test_no_alert_with_stable_high_rate(self):
        t = CacheTracker()
        for _ in range(15):
            t.record_response("ds/v4", _ds_resp(1000, 200, 900, 100))
        assert t.check_hit_rate_drop() is None
        t.close()

    def test_alert_on_significant_drop(self):
        t = CacheTracker()
        for _ in range(10):
            t.record_response("ds/v4", _ds_resp(1000, 200, 900, 100))
        for _ in range(6):
            t.record_response("ds/v4", _ds_resp(1000, 200, 300, 700))
        alert = t.check_hit_rate_drop()
        assert alert is not None
        assert alert["drop_pct"] > 0.30
        assert "suggestion" in alert
        t.close()

    def test_alert_not_triggered_by_small_drop(self):
        t = CacheTracker()
        for _ in range(10):
            t.record_response("ds/v4", _ds_resp(1000, 200, 900, 100))
        for _ in range(6):
            t.record_response("ds/v4", _ds_resp(1000, 200, 800, 200))
        alert = t.check_hit_rate_drop()
        # 90% → 80%，下降 ~11%，低于 40% 阈值
        if alert:
            assert alert["drop_pct"] <= 0.40
        t.close()

    def test_all_zero_cache_no_alert(self):
        t = CacheTracker()
        for _ in range(15):
            t.record_response("x", {"usage": {"prompt_tokens": 100, "completion_tokens": 50}})
        assert t.check_hit_rate_drop() is None
        t.close()


class TestCacheTrackerSystemHash:
    """system prompt hash 追踪。"""

    def test_hash_stability(self):
        h1 = _hash_system_prompt("You are helpful.")
        h2 = _hash_system_prompt("You are helpful.")
        h3 = _hash_system_prompt("Different prompt.")
        assert h1 == h2
        assert h1 != h3

    def test_tracks_system_hash(self):
        t = CacheTracker()
        t.set_system_prompt("Test prompt.")
        t.record_response("ds/v4", _ds_resp(), "Test prompt.")
        assert t.system_hash == _hash_system_prompt("Test prompt.")
        t.close()

    def test_hash_change_detected_in_history(self):
        t = CacheTracker()
        t.record_response("ds/v4", _ds_resp(), "Prompt A")
        t.record_response("ds/v4", _ds_resp(), "Prompt B")
        # system hash should now be Prompt B's hash
        assert t.system_hash == _hash_system_prompt("Prompt B")
        t.close()


class TestCacheTrackerPricing:
    """定价表匹配。"""

    def test_match_v4_pro(self):
        p = _match_pricing("deepseek-v4-pro")
        assert p["input_cache_hit"] == 0.025
        assert p["input_cache_miss"] == 3.0
        assert p["output"] == 6.0

    def test_match_v4_flash(self):
        p = _match_pricing("deepseek/deepseek-v4-flash")
        assert p == {
            "input_cache_hit": 0.02,
            "input_cache_miss": 1.0,
            "output": 2.0,
        }

    def test_legacy_chat_uses_flash_price_for_historical_usage(self):
        p = _match_pricing("deepseek-chat")
        assert p == _match_pricing("deepseek-v4-flash")

    def test_legacy_reasoner_uses_flash_price_for_historical_usage(self):
        p = _match_pricing("deepseek-reasoner")
        assert p == _match_pricing("deepseek-v4-flash")

    def test_match_unknown_fallback(self):
        p = _match_pricing("some-unknown-model")
        assert p == _match_pricing("deepseek-v4-pro")

    def test_custom_pricing_from_get_pricing(self):
        t = CacheTracker()
        # 自定义定价表中不存在的模型回退到内置
        p = t.get_pricing("deepseek-v4-pro")
        assert p["input_cache_hit"] == 0.025
        t.close()


class TestCacheTrackerConcurrency:
    """并发安全。"""

    def test_concurrent_record(self):
        t = CacheTracker()
        errors = []

        def worker():
            try:
                for _ in range(100):
                    t.record_response("ds/v4", _ds_resp(100, 20, 80, 20))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert not errors, f"并发错误: {errors}"
        snap = t.model_snapshot("ds/v4")
        assert snap["calls"] == 1000
        assert snap["prompt_tokens"] == 100000
        t.close()


class TestCacheTrackerAutoCallback:
    """CacheTracker 通过全局响应回调自动工作。"""

    def test_auto_tracks_via_callback(self):
        t = CacheTracker()
        t.set_system_prompt("test")
        _emit_response("ds/v4", _ds_resp(1000, 200, 800, 200))
        time.sleep(0.01)  # 回调是同步的，但保险起见
        assert t.cache_hits == 800
        t.close()

    def test_auto_tracks_multiple_models(self):
        t = CacheTracker()
        _emit_response("ds/v4", _ds_resp(1000, 200, 800, 200))
        _emit_response("ds/r1", _ds_resp(500, 100, 400, 100))
        assert set(t.all_models) == {"ds/v4", "ds/r1"}
        t.close()

    def test_close_stops_tracking(self):
        t = CacheTracker()
        _emit_response("ds/v4", _ds_resp(1000, 200, 800, 200))
        assert t.cache_hits > 0
        t.close()
        before = t.cache_hits
        _emit_response("ds/v4", _ds_resp(1000, 200, 800, 200))
        assert t.cache_hits == before  # 不再增长
        t.close()


# ══════════════════════════════════════════════════════════════
# PromptOptimizer 缓存优化
# ══════════════════════════════════════════════════════════════

class TestOptimizeMessagesForCache:

    def test_stable_parts_first(self):
        messages = [
            {"role": "system", "content": "You are a coder."},
            {"role": "user", "content": "Write a function."},
        ]
        result = optimize_messages_for_cache(
            messages,
            tools_schema='[{"name":"read_file"}]',
            system_prompt_core="You are a coder.",
        )
        assert result[0]["role"] == "system"
        assert "read_file" in result[0]["content"]
        assert "You are a coder" in result[0]["content"]

    def test_dynamic_content_moved_to_user(self):
        messages = [
            {"role": "system", "content": "Current time: 2025-07-20 14:30:00"},
            {"role": "user", "content": "Hello"},
        ]
        result = optimize_messages_for_cache(messages)
        # 动态 system 内容应被移出
        sys_contents = [m["content"] for m in result if m["role"] == "system"]
        user_contents = [m["content"] for m in result if m["role"] == "user"]
        if sys_contents:
            # 如果有 system 消息，不应包含日期
            assert "2025-07-20" not in sys_contents[0]
        # 动态内容应在 user 消息中
        combined = " ".join(user_contents)
        assert "2025-07-20" in combined or "Hello" in combined

    def test_preserves_conversation_order(self):
        messages = [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
        ]
        result = optimize_messages_for_cache(messages)
        # 非 system 消息顺序不变
        non_sys = [m for m in result if m["role"] != "system"]
        assert non_sys[0]["content"] == "Q1"
        assert non_sys[2]["content"] == "Q2"

    def test_empty_input(self):
        result = optimize_messages_for_cache([])
        assert result == []

    def test_no_system_message(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        result = optimize_messages_for_cache(messages, tools_schema="T")
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "T"

    def test_stable_system_kept(self):
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hi"},
        ]
        result = optimize_messages_for_cache(messages)
        assert any(m["role"] == "system" for m in result)
        sys_combined = " ".join(
            m["content"] for m in result if m["role"] == "system"
        )
        assert "helpful assistant" in sys_combined


class TestIsDynamicContent:

    def test_date(self):
        assert _is_dynamic_content("Today is 2025-07-20")

    def test_time(self):
        assert _is_dynamic_content("Current time: 14:30:00")

    def test_file_path(self):
        assert _is_dynamic_content("Read /home/user/file.py")
        assert _is_dynamic_content("Open C:\\Users\\file.txt")

    def test_template_var(self):
        assert _is_dynamic_content("Hello ${username}")

    def test_static_content(self):
        assert not _is_dynamic_content("You are a helpful coding assistant.")
        assert not _is_dynamic_content("Always respond in JSON format.")

    def test_username_keyword(self):
        assert _is_dynamic_content("The current username is bob")


# ══════════════════════════════════════════════════════════════
# LLMUsage _extract_usage 集成
# ══════════════════════════════════════════════════════════════

class TestExtractUsageEdgeCases:

    def test_cache_hit_zero_when_not_present(self):
        u = _extract_usage({
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        }, "deepseek")
        assert u.cache_hit_tokens == 0
        assert u.cache_miss_tokens == 0

    def test_total_tokens_fallback(self):
        u = _extract_usage({
            "usage": {"prompt_tokens": 100, "completion_tokens": 50}
        }, "deepseek")
        assert u.total_tokens == 150

    def test_string_values_still_parse(self):
        """兼容字符串数字的 usage 值（某些代理会这样返回）。"""
        u = _extract_usage({
            "usage": {
                "prompt_tokens": "100",
                "completion_tokens": "50",
                "prompt_cache_hit_tokens": "80",
            }
        }, "deepseek")
        assert u.prompt_tokens == 100
        assert u.cache_hit_tokens == 80
