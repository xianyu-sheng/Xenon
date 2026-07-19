"""Chaos Test 1: 网络中断模拟。

目标：验证 ``xenon.utils.llm_client.chat_completion`` 在 httpx 抛
``ConnectError`` 时能正确重试，且 2 次失败后第 3 次成功也能完成调用。
本测试**不调真实 LLM**——只 unit-test 风格 mock httpx.Client.post。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

import xenon.utils.llm_client as llm_client


def _make_response(text: str = "hello") -> MagicMock:
    """构造一个 OpenAI 兼容格式的 mock Response。"""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [
            {"message": {"content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    resp.raise_for_status = MagicMock()
    return resp


def test_network_outage_retries(monkeypatch):
    """前 2 次 ConnectError，第三次成功 → chat_completion 应返回结果。"""
    call_count = {"n": 0}

    def fake_post(url, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            raise httpx.ConnectError("simulated network failure")
        return _make_response("recovered after retry")

    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.post = fake_post
    fake_client.close = MagicMock()

    monkeypatch.setattr(llm_client, "_get_pooled_client", lambda *a, **kw: fake_client)
    monkeypatch.setattr(llm_client, "build_endpoint", lambda *a, **kw: MagicMock(
        provider="openai", model_name="gpt-4o", base_url="https://api.test/v1",
        api_key="sk-test", max_tokens=4096,
    ))

    # max_retries=3，第三次能成功
    result = llm_client.chat_completion(
        "openai/gpt-4o",
        [{"role": "user", "content": "hi"}],
        max_retries=3,
    )
    assert result == "recovered after retry"
    assert call_count["n"] == 3, f"应重试 3 次（2 失败 + 1 成功），实际 {call_count['n']}"


def test_network_outage_exhausted_raises(monkeypatch):
    """持续 ConnectError 超过 max_retries → 应抛最后一次的 ConnectError。"""
    def fake_post(url, **kwargs):
        raise httpx.ConnectError("permanent failure")

    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.post = fake_post
    fake_client.close = MagicMock()

    monkeypatch.setattr(llm_client, "_get_pooled_client", lambda *a, **kw: fake_client)
    monkeypatch.setattr(llm_client, "build_endpoint", lambda *a, **kw: MagicMock(
        provider="openai", model_name="gpt-4o", base_url="https://api.test/v1",
        api_key="sk-test", max_tokens=4096,
    ))

    with pytest.raises(httpx.ConnectError):
        llm_client.chat_completion(
            "openai/gpt-4o",
            [{"role": "user", "content": "hi"}],
            max_retries=2,
        )
