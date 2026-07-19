"""P3-Q1 续 验收：ContextManager 用真实 usage 替代估算 + 流式 usage 提取（§8.8.1）。

Q1 第一步（chat_completion 经 usage 回调发出 LLMUsage）已落地于 test_q1_usage。
本文件覆盖第二步：

- ContextManager.record_real_usage / track_real_usage 订阅。
- current_token_usage() 优先真实 total_tokens，无则回退启发式。
- undo / clear / compact 失效真实 usage；compact 期间抑制自身摘要调用。
- stats() 暴露 token_source / real_usage。
- close() 退订。
- 流式路径（_stream_openai_compat / _stream_anthropic）提取 usage 并经回调发出。
"""

from __future__ import annotations

from typing import Any

import pytest

import xenon.utils.llm_client as lc
from xenon.repl.context_manager import ContextManager
from xenon.utils.llm_client import (
    LLMUsage,
    ModelEndpoint,
    _extract_usage,
    register_usage_callback,
)


def _endpoint(provider: str) -> ModelEndpoint:
    base = "https://api.anthropic.com" if provider == "anthropic" else "https://api.openai.com/v1"
    return ModelEndpoint(provider=provider, model_name="m", base_url=base, api_key="k")


# ── ContextManager 真实 usage ───────────────────────────────


class TestRealUsagePrecedence:
    def test_record_overrides_heuristic(self):
        cm = ContextManager()
        cm.add_user_message("一段较长的对话内容用于产生非零启发式估算")
        heuristic = cm.current_token_usage()
        assert heuristic > 0

        cm.record_real_usage(10, 5, 15)
        assert cm.current_token_usage() == 15  # 真实优先

    def test_falls_back_to_heuristic_without_real(self):
        cm = ContextManager()
        cm.add_user_message("hello world 对话")
        # 无真实 usage → 启发式
        assert cm.current_token_usage() == sum(t.token_count for t in cm.history)

    def test_real_takes_precedence_over_large_heuristic(self):
        cm = ContextManager()
        for i in range(20):
            cm.add_user_message(f"消息编号 {i} " * 10)
        assert cm.current_token_usage() > 100  # 启发式较大

        cm.record_real_usage(3, 2, 5)
        assert cm.current_token_usage() == 5  # 真实覆盖

    def test_total_defaults_to_sum(self):
        cm = ContextManager()
        cm.record_real_usage(10, 5)  # 不传 total
        assert cm.current_token_usage() == 15
        assert cm.real_usage() == {"prompt": 10, "completion": 5, "total": 15}

    def test_empty_history_zero(self):
        cm = ContextManager()
        assert cm.current_token_usage() == 0
        assert cm.real_usage() is None


class TestInvalidation:
    def test_undo_invalidates(self):
        cm = ContextManager()
        cm.add_user_message("first")
        cm.save_snapshot()
        cm.add_user_message("second")
        cm.record_real_usage(100, 50, 150)
        assert cm.current_token_usage() == 150

        assert cm.undo() is True
        assert cm.real_usage() is None  # 失效
        # 回退到启发式
        assert cm.current_token_usage() == sum(t.token_count for t in cm.history)

    def test_clear_invalidates(self):
        cm = ContextManager()
        cm.add_user_message("x")
        cm.record_real_usage(10, 5, 15)
        cm.clear()
        assert cm.real_usage() is None
        assert cm.current_token_usage() == 0

    def test_compact_invalidates(self):
        """Tier 3 安全截断（不调 LLM）后真实 usage 失效。"""
        cm = ContextManager(max_tokens=100, compact_threshold=0.6, compact_force=0.85)
        for i in range(30):
            cm.add_user_message(f"消息内容 {i} " * 5)
        cm.record_real_usage(999, 1, 1000)
        assert cm.real_usage() is not None

        cm.compact()  # ratio 高 → Tier 3 安全截断
        assert cm.real_usage() is None  # 失效


class TestSuppressDuringCompact:
    def test_suppress_flag_blocks_recording(self):
        cm = ContextManager()
        cm._suppress_usage = True
        cm._on_usage("m", LLMUsage(10, 5, 15), 0.1)
        assert cm.real_usage() is None  # 被抑制

    def test_unsuppress_allows_recording(self):
        cm = ContextManager()
        cm._suppress_usage = False
        cm._on_usage("m", LLMUsage(10, 5, 15), 0.1)
        assert cm.real_usage() == {"prompt": 10, "completion": 5, "total": 15}


class TestSubscription:
    def test_track_real_usage_subscribes(self):
        cm = ContextManager(track_real_usage=True)
        try:
            assert cm._usage_unsub is not None
            # 经全局回调发出 → ctx_mgr 应记录
            lc._emit_usage("openai/gpt-4o", LLMUsage(12, 8, 20), 0.05)
            assert cm.real_usage() == {"prompt": 12, "completion": 8, "total": 20}
            assert cm.current_token_usage() == 20
        finally:
            cm.close()

    def test_close_unsubscribes(self):
        cm = ContextManager(track_real_usage=True)
        cm.close()
        assert cm._usage_unsub is None
        # close 后再发 usage → 不再记录
        lc._emit_usage("m", LLMUsage(1, 1, 2), 0.0)
        assert cm.real_usage() is None

    def test_track_off_by_default(self):
        cm = ContextManager()
        assert cm._usage_unsub is None
        lc._emit_usage("m", LLMUsage(1, 1, 2), 0.0)
        assert cm.real_usage() is None  # 未订阅，不记录


class TestStats:
    def test_stats_heuristic_source(self):
        cm = ContextManager()
        cm.add_user_message("hi")
        s = cm.stats()
        assert s["token_source"] == "heuristic"
        assert s["real_usage"] is None
        assert s["estimated_tokens"] > 0

    def test_stats_real_source(self):
        cm = ContextManager()
        cm.add_user_message("hi")
        cm.record_real_usage(10, 5, 15)
        s = cm.stats()
        assert s["token_source"] == "real"
        assert s["real_usage"] == {"prompt": 10, "completion": 5, "total": 15}
        assert s["estimated_tokens"] == 15


# ── 流式 usage 提取 ─────────────────────────────────────────


class _FakeStreamResp:
    def __init__(self, lines: list[str]):
        self._lines = lines

    def raise_for_status(self) -> None:
        pass

    def iter_lines(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeStreamClient:
    def __init__(self, lines: list[str]):
        self._lines = lines

    def stream(self, method, url, **kw):
        return _FakeStreamResp(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_stream(monkeypatch, provider: str, lines: list[str]):
    monkeypatch.setattr(lc, "_create_http_client", lambda timeout=120: _FakeStreamClient(lines))
    monkeypatch.setattr(lc, "build_endpoint", lambda mid, c=None, b=None: _endpoint(provider))


class TestStreamUsageOpenAI:
    def test_emits_usage_from_final_chunk(self, monkeypatch):
        seen: list[tuple[str, LLMUsage, float]] = []
        unsub = register_usage_callback(lambda m, u, lat: seen.append((m, u, lat)))
        lines = [
            'data: {"choices":[{"delta":{"content":"he"}}]}',
            'data: {"choices":[{"delta":{"content":"llo"}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":8,"total_tokens":20}}',
            "data: [DONE]",
        ]
        _patch_stream(monkeypatch, "openai", lines)
        try:
            chunks = list(lc.chat_completion_stream(
                "openai/gpt-4o", [{"role": "user", "content": "hi"}],
                credentials={"openai": "sk-test"}))
        finally:
            unsub()

        assert "".join(chunks) == "hello"
        assert len(seen) == 1
        model, usage, latency = seen[0]
        assert model == "openai/gpt-4o"
        assert usage.prompt_tokens == 12
        assert usage.completion_tokens == 8
        assert usage.total_tokens == 20
        assert latency >= 0.0

    def test_no_usage_no_emit(self, monkeypatch):
        seen: list = []
        unsub = register_usage_callback(lambda m, u, lat: seen.append((m, u, lat)))
        lines = [
            'data: {"choices":[{"delta":{"content":"hi"}}]}',
            "data: [DONE]",
        ]
        _patch_stream(monkeypatch, "openai", lines)
        try:
            list(lc.chat_completion_stream("openai/gpt-4o", [{"role": "user", "content": "hi"}],
                                           credentials={"openai": "sk-test"}))
        finally:
            unsub()
        assert seen == []  # 无 usage chunk → 不 emit


class TestStreamUsageAnthropic:
    def test_emits_usage_from_events(self, monkeypatch):
        seen: list[tuple[str, LLMUsage, float]] = []
        unsub = register_usage_callback(lambda m, u, lat: seen.append((m, u, lat)))
        lines = [
            'data: {"type":"message_start","message":{"usage":{"input_tokens":7,"output_tokens":0}}}',
            'data: {"type":"content_block_delta","delta":{"text":"hi back"}}',
            'data: {"type":"message_delta","usage":{"output_tokens":4}}',
        ]
        _patch_stream(monkeypatch, "anthropic", lines)
        try:
            chunks = list(lc.chat_completion_stream(
                "anthropic/claude-3-5-sonnet", [{"role": "user", "content": "hi"}],
                credentials={"anthropic": "sk-test"}))
        finally:
            unsub()

        assert "".join(chunks) == "hi back"
        assert len(seen) == 1
        model, usage, latency = seen[0]
        assert model == "anthropic/claude-3-5-sonnet"
        assert usage.prompt_tokens == 7  # input_tokens
        assert usage.completion_tokens == 4  # message_delta output_tokens
        assert usage.total_tokens == 11
