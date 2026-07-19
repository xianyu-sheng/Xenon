"""Chaos Test 3: LLM 返回 429 限速。

目标：验证 xenon 的两层重试机制：
1. ``chat_completion`` 内层对单次 429 指数退避重试（1s, 2s, 4s）；
2. ``BaseEngine._call_llm`` 在多模型间 fallback 切换。

本测试不调真实 LLM，只 unit-test mock httpx Response 模拟 429。
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import httpx
import pytest

import xenon.utils.llm_client as llm_client

def _make_429_response() -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 429
    resp.headers = {"Retry-After": "0"}  # 关闭 sleep
    # raise_for_status 抛 HTTPStatusError
    def _raise():
        request = MagicMock()
        raise httpx.HTTPStatusError(
            "429 Too Many Requests", request=request, response=resp
        )
    resp.raise_for_status = _raise
    return resp


def _make_200_response(text: str = "ok") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    resp.raise_for_status = MagicMock()
    return resp


def test_429_retries_then_succeeds(monkeypatch):
    """2 次 429 + 第 3 次 200 → chat_completion 应返回成功结果。"""
    call_count = {"n": 0}

    def fake_post(url, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return _make_429_response()
        return _make_200_response("ok after 2x429")

    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.post = fake_post
    fake_client.close = MagicMock()

    monkeypatch.setattr(llm_client, "_get_pooled_client", lambda *a, **kw: fake_client)
    monkeypatch.setattr(llm_client, "build_endpoint", lambda *a, **kw: MagicMock(
        provider="openai", model_name="gpt-4o", base_url="https://api.test/v1",
        api_key="sk-test", max_tokens=4096,
    ))

    # 跳过真实 sleep：patch 全局 time.sleep（chat_completion 内 ``import time``
    # 引用的是同一 sys.modules['time']，所以 patch 全局即可）
    monkeypatch.setattr(time, "sleep", lambda s: None)

    result = llm_client.chat_completion(
        "openai/gpt-4o",
        [{"role": "user", "content": "hi"}],
        max_retries=3,
    )
    assert result == "ok after 2x429"
    assert call_count["n"] == 3


def test_429_exhausted_raises(monkeypatch):
    """max_retries 次 429 后应抛 HTTPStatusError。"""
    def fake_post(url, **kwargs):
        return _make_429_response()

    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.post = fake_post
    fake_client.close = MagicMock()

    monkeypatch.setattr(llm_client, "_get_pooled_client", lambda *a, **kw: fake_client)
    monkeypatch.setattr(llm_client, "build_endpoint", lambda *a, **kw: MagicMock(
        provider="openai", model_name="gpt-4o", base_url="https://api.test/v1",
        api_key="sk-test", max_tokens=4096,
    ))
    monkeypatch.setattr(time, "sleep", lambda s: None)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        llm_client.chat_completion(
            "openai/gpt-4o",
            [{"role": "user", "content": "hi"}],
            max_retries=2,
        )
    assert exc_info.value.response.status_code == 429


def test_429_then_5xx_then_success(monkeypatch):
    """混合错误：1 次 429 + 1 次 500 + 1 次 200 → 应最终成功。"""
    call_count = {"n": 0}

    def fake_post(url, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _make_429_response()
        elif call_count["n"] == 2:
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 503
            def _raise():
                raise httpx.HTTPStatusError("503", request=MagicMock(), response=resp)
            resp.raise_for_status = _raise
            return resp
        return _make_200_response("ok")

    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.post = fake_post
    fake_client.close = MagicMock()

    monkeypatch.setattr(llm_client, "_get_pooled_client", lambda *a, **kw: fake_client)
    monkeypatch.setattr(llm_client, "build_endpoint", lambda *a, **kw: MagicMock(
        provider="openai", model_name="gpt-4o", base_url="https://api.test/v1",
        api_key="sk-test", max_tokens=4096,
    ))
    monkeypatch.setattr(time, "sleep", lambda s: None)

    result = llm_client.chat_completion(
        "openai/gpt-4o",
        [{"role": "user", "content": "hi"}],
        max_retries=3,
    )
    assert result == "ok"
    assert call_count["n"] == 3


def test_429_exponential_backoff_delays(monkeypatch):
    """验证 429 重试的实际退避延迟符合 2^attempt 公式。"""
    delays: list[float] = []

    def fake_post(url, **kwargs):
        return _make_429_response()

    def fake_sleep(s):
        delays.append(s)

    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.post = fake_post
    fake_client.close = MagicMock()

    monkeypatch.setattr(llm_client, "_get_pooled_client", lambda *a, **kw: fake_client)
    monkeypatch.setattr(llm_client, "build_endpoint", lambda *a, **kw: MagicMock(
        provider="openai", model_name="gpt-4o", base_url="https://api.test/v1",
        api_key="sk-test", max_tokens=4096,
    ))
    monkeypatch.setattr(time, "sleep", fake_sleep)

    with pytest.raises(httpx.HTTPStatusError):
        llm_client.chat_completion(
            "openai/gpt-4o",
            [{"role": "user", "content": "hi"}],
            max_retries=3,
        )
    # max_retries=3 共 3 次尝试，每次失败后 sleep：
    # attempt 0 → 1s, attempt 1 → 2s, attempt 2 → 4s（最后一次失败后 sleep）
    assert delays == [1, 2, 4], f"应符合 2^n 退避，实际 {delays}"
