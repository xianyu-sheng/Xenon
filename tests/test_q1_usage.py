"""
P3-Q1 chat_completion usage/延迟统计测试（§8.8.1）。

验证：
- LLM 响应的 usage 不再被丢弃——经 usage 回调发出 (model_id, LLMUsage, latency)。
- OpenAI 兼容与 Anthropic 两种 usage 字段格式都能正确归一化。
- chat_completion 返回 str 的契约不变（向后兼容）。
- UsageTracker 累计真实 token / 延迟 / 调用次数。
- 续写（多次 once）usage 累加。
- 无 usage 字段时不崩溃。
- 回调异常被隔离。
"""

from __future__ import annotations

from typing import Any

import httpx

import xenon.utils.llm_client as lc
from xenon.utils.llm_client import (
    LLMResponse,
    LLMUsage,
    ModelEndpoint,
    UsageTracker,
    _extract_usage,
    register_usage_callback,
)


def _endpoint(provider: str) -> ModelEndpoint:
    base = "https://api.anthropic.com" if provider == "anthropic" else "https://api.openai.com/v1"
    return ModelEndpoint(provider=provider, model_name="m", base_url=base, api_key="k")


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://x")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=req,
                response=httpx.Response(self.status_code, request=req),
            )

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """返回预设 payload（单个或序列）。"""

    def __init__(self, payload=None, payloads=None):
        self._payload = payload
        self._payloads = list(payloads) if payloads else None

    def post(self, url, *, json=None, headers=None, timeout=None):
        if self._payloads is not None:
            return _FakeResponse(self._payloads.pop(0))
        return _FakeResponse(self._payload)

    def close(self):
        pass


def _patch_provider(monkeypatch, provider: str, fake: _FakeClient) -> None:
    monkeypatch.setattr(lc, "_get_pooled_client", lambda endpoint, timeout=120: fake)
    monkeypatch.setattr(lc, "build_endpoint", lambda mid, c=None, b=None: _endpoint(provider))


# ── _extract_usage 归一化 ────────────────────────────────────


class TestExtractUsage:
    def test_openai_compat_format(self):
        data = {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        u = _extract_usage(data, "openai")
        assert u.prompt_tokens == 10
        assert u.completion_tokens == 5
        assert u.total_tokens == 15

    def test_anthropic_format(self):
        data = {"usage": {"input_tokens": 7, "output_tokens": 3}}
        u = _extract_usage(data, "anthropic")
        assert u.prompt_tokens == 7
        assert u.completion_tokens == 3
        assert u.total_tokens == 10  # input + output

    def test_missing_usage_returns_zero(self):
        u = _extract_usage({"choices": []}, "openai")
        assert u.total_tokens == 0

    def test_none_data(self):
        assert _extract_usage(None, "openai").total_tokens == 0

    def test_partial_usage_fields(self):
        # 缺 total_tokens → 用 prompt+completion 求和
        u = _extract_usage({"usage": {"prompt_tokens": 4, "completion_tokens": 6}}, "openai")
        assert u.total_tokens == 10


# ── chat_completion 发出 usage 回调 ─────────────────────────


class TestChatCompletionEmitsUsage:
    def test_openai_usage_emitted_with_latency(self, monkeypatch):
        seen: list[tuple[str, LLMUsage, float]] = []
        unsub = register_usage_callback(lambda m, u, lat: seen.append((m, u, lat)))
        fake = _FakeClient(payload={
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        })
        _patch_provider(monkeypatch, "openai", fake)
        try:
            text = lc.chat_completion("openai/gpt-4o", [{"role": "user", "content": "hi"}],
                                      credentials={"openai": "sk-test"})
        finally:
            unsub()

        assert text == "hello"  # 返回 str 契约不变
        assert len(seen) == 1
        model, usage, latency = seen[0]
        assert model == "openai/gpt-4o"
        assert usage.prompt_tokens == 12
        assert usage.completion_tokens == 8
        assert usage.total_tokens == 20
        assert latency >= 0.0

    def test_anthropic_usage_emitted(self, monkeypatch):
        seen: list[tuple[str, LLMUsage, float]] = []
        unsub = register_usage_callback(lambda m, u, lat: seen.append((m, u, lat)))
        fake = _FakeClient(payload={
            "content": [{"type": "text", "text": "hi back"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 4},
        })
        _patch_provider(monkeypatch, "anthropic", fake)
        try:
            text = lc.chat_completion("anthropic/claude-3-5-sonnet",
                                      [{"role": "user", "content": "hi"}],
                                      credentials={"anthropic": "sk-test"})
        finally:
            unsub()

        assert text == "hi back"
        assert len(seen) == 1
        _, usage, _ = seen[0]
        assert usage.prompt_tokens == 5
        assert usage.completion_tokens == 4
        assert usage.total_tokens == 9

    def test_no_usage_field_still_emits_zero(self, monkeypatch):
        seen: list[tuple[str, LLMUsage, float]] = []
        unsub = register_usage_callback(lambda m, u, lat: seen.append((m, u, lat)))
        fake = _FakeClient(payload={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            # 无 usage 字段
        })
        _patch_provider(monkeypatch, "openai", fake)
        try:
            lc.chat_completion("openai/gpt-4o", [{"role": "user", "content": "hi"}],
                               credentials={"openai": "sk-test"})
        finally:
            unsub()

        assert len(seen) == 1
        assert seen[0][1].total_tokens == 0  # 不崩溃，零 usage

    def test_continuation_accumulates_usage(self, monkeypatch):
        """续写多次 once 的 usage 应累加为一次 chat_completion 的总量。"""
        seen: list[tuple[str, LLMUsage, float]] = []
        unsub = register_usage_callback(lambda m, u, lat: seen.append((m, u, lat)))
        fake = _FakeClient(payloads=[
            {
                "choices": [{"message": {"content": "part1"}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
            {
                "choices": [{"message": {"content": "part2"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27},
            },
        ])
        _patch_provider(monkeypatch, "openai", fake)
        try:
            text = lc.chat_completion("openai/gpt-4o", [{"role": "user", "content": "hi"}],
                                      credentials={"openai": "sk-test"})
        finally:
            unsub()

        assert text == "part1part2"
        assert len(seen) == 1  # 一次 chat_completion = 一次 usage 事件
        _, usage, _ = seen[0]
        assert usage.prompt_tokens == 30  # 10 + 20
        assert usage.completion_tokens == 12  # 5 + 7
        assert usage.total_tokens == 42  # 15 + 27


# ── UsageTracker 累计 ────────────────────────────────────────


class TestUsageTracker:
    def test_tracker_accumulates(self, monkeypatch):
        tracker = UsageTracker()
        fake = _FakeClient(payload={
            "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        })
        _patch_provider(monkeypatch, "openai", fake)
        try:
            lc.chat_completion("openai/gpt-4o", [{"role": "user", "content": "hi"}],
                               credentials={"openai": "sk-test"})
            lc.chat_completion("openai/gpt-4o", [{"role": "user", "content": "hi"}],
                               credentials={"openai": "sk-test"})

            snap = tracker.snapshot()
            assert "openai/gpt-4o" in snap
            assert snap["openai/gpt-4o"]["calls"] == 2
            assert snap["openai/gpt-4o"]["prompt_tokens"] == 200
            assert snap["openai/gpt-4o"]["completion_tokens"] == 100
            assert snap["openai/gpt-4o"]["total_tokens"] == 300
            assert snap["openai/gpt-4o"]["latency_avg"] >= 0.0
            assert tracker.total_tokens() == 300
            assert tracker.total_calls() == 2
        finally:
            tracker.close()

    def test_tracker_close_unsubscribes(self, monkeypatch):
        tracker = UsageTracker()
        tracker.close()
        fake = _FakeClient(payload={
            "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })
        _patch_provider(monkeypatch, "openai", fake)
        lc.chat_completion("openai/gpt-4o", [{"role": "user", "content": "hi"}],
                           credentials={"openai": "sk-test"})
        # close 后不再累计
        assert tracker.total_tokens() == 0

    def test_callback_exception_isolated(self, monkeypatch):
        """回调抛异常不应影响主调用链。"""
        def bad_cb(m, u, lat):
            raise RuntimeError("boom")
        unsub = register_usage_callback(bad_cb)
        ok: list[int] = []
        unsub2 = register_usage_callback(lambda m, u, lat: ok.append(u.total_tokens))
        fake = _FakeClient(payload={
            "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })
        _patch_provider(monkeypatch, "openai", fake)
        try:
            text = lc.chat_completion("openai/gpt-4o", [{"role": "user", "content": "hi"}],
                                      credentials={"openai": "sk-test"})
        finally:
            unsub()
            unsub2()

        assert text == "x"  # 主调用未受影响
        assert ok == [2]  # 其它回调仍被调用


# ── LLMResponse（tools 路径）仍保留 raw 中的 usage ─────────


class TestLLMResponseRaw:
    def test_llmresponse_has_tool_calls_property(self):
        r = LLMResponse(content="x", tool_calls=[{"id": "1", "name": "f", "arguments": {}}])
        assert r.has_tool_calls is True
        r2 = LLMResponse(content="x")
        assert r2.has_tool_calls is False
