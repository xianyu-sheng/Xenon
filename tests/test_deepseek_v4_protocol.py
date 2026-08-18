"""DeepSeek V4 thinking-mode tool-call protocol regression tests."""

from __future__ import annotations

import json

import pytest

from xenon.engine.base import BaseEngine
from xenon.engine.context import AgentContext
from xenon.engine.react_engine import ReActEngine
from xenon.repl.context_manager import ContextManager
from xenon.utils.llm_client import LLMResponse, _messages_for_anthropic


class _ConcreteEngine(BaseEngine):
    def run(self, user_input, context=None, ctx_mgr=None):
        return ""


def _native_response() -> LLMResponse:
    message = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "I need the current time before answering.",
        "tool_calls": [{
            "id": "call_time",
            "type": "function",
            "function": {"name": "datetime", "arguments": "{}"},
        }],
    }
    return LLMResponse(
        reasoning_content=message["reasoning_content"],
        tool_calls=[{"id": "call_time", "name": "datetime", "arguments": {}}],
        finish_reason="tool_calls",
        provider="deepseek",
        assistant_message=message,
    )


def test_native_protocol_preserves_reasoning_and_tool_call_id():
    engine = _ConcreteEngine(["deepseek/deepseek-v4-pro"])
    engine._pending_native_response = _native_response()

    messages = engine._consume_native_tool_messages(["2026-07-21 12:00"])

    assert messages[0]["reasoning_content"].startswith("I need")
    assert messages[0]["tool_calls"][0]["id"] == "call_time"
    assert messages[1] == {
        "role": "tool",
        "tool_call_id": "call_time",
        "content": "2026-07-21 12:00",
    }
    assert engine._last_provider_messages == messages


def test_react_auto_enables_native_fc_for_canonical_deepseek_v4():
    assert ReActEngine(["deepseek/deepseek-v4-pro"]).native_fc is True
    assert ReActEngine(["deepseek/deepseek-v4-flash"]).native_fc is True
    assert ReActEngine(["ark/deepseek-v4-pro-260425"]).native_fc is True
    assert ReActEngine(["custom/deepseek-v4-flash-260425"]).native_fc is True
    assert ReActEngine(["deepseek/deepseek-v4-pro"], native_fc=False).native_fc is False
    assert ReActEngine(["openai/gpt-4o"]).native_fc is False


def test_provider_protocol_replays_in_memory_but_not_raw_in_session_export():
    manager = ContextManager()
    protocol = _ConcreteEngine(["m"])
    protocol._pending_native_response = _native_response()
    messages = protocol._consume_native_tool_messages(["ok"])

    assert manager.add_provider_messages(messages) == 2
    assert manager.get_messages() == messages

    exported = manager.export_history()
    assert all("api_message" not in item["metadata"] for item in exported)
    assert exported[0]["content"].startswith("[原生工具调用")


def test_openai_tool_history_converts_to_anthropic_blocks():
    canonical = [{"role": "system", "content": "system"}]
    engine = _ConcreteEngine(["m"])
    engine._pending_native_response = _native_response()
    canonical.extend(engine._consume_native_tool_messages(["tool output"]))

    system, messages = _messages_for_anthropic(canonical)

    assert system == "system"
    assert messages[0]["content"][0]["type"] == "tool_use"
    assert messages[1]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "call_time",
        "content": "tool output",
    }


def test_react_sends_complete_deepseek_tool_protocol_on_next_iteration(monkeypatch):
    engine = ReActEngine(
        ["deepseek/deepseek-v4-pro"],
        max_iterations=3,
        native_fc=True,
    )
    seen: list[list[dict]] = []

    def fake_call(messages, tools, response_format, max_tokens=None):
        seen.append(list(messages))
        if len(seen) == 1:
            return _native_response()
        assistant_index = next(
            index for index, message in enumerate(messages)
            if message.get("tool_calls")
        )
        assistant = messages[assistant_index]
        tool_result = messages[assistant_index + 1]
        assert assistant["reasoning_content"].startswith("I need")
        assert assistant["tool_calls"][0]["id"] == "call_time"
        assert tool_result["role"] == "tool"
        assert tool_result["tool_call_id"] == "call_time"
        return LLMResponse(content=json.dumps({"final_answer": "done"}))

    monkeypatch.setattr(engine, "_call_with_tools_once", fake_call)

    result = engine.run("现在几点？", AgentContext())

    assert result == "done"
    assert len(engine._last_provider_messages) == 2


def test_native_all_models_failed_falls_back_to_text_protocol(monkeypatch):
    """SWE-bench 实测修复：native 请求全模型 5xx 时不再让引擎崩溃，
    熔断 native 并整体回退文本协议（_call_llm + chain_retries 重试）。"""
    engine = ReActEngine(
        ["deepseek/deepseek-v4-pro"],
        max_iterations=3,
        native_fc=True,
    )
    text_calls = {"n": 0}

    def fail_native(messages, tools, response_format, max_tokens=None):
        raise RuntimeError("native provider request failed: Server error '500'")

    def fake_text(messages, max_tokens=None, **kwargs):
        text_calls["n"] += 1
        return '{"thought": "t", "final_answer": "文本协议成功"}'

    monkeypatch.setattr(engine, "_call_with_tools_once", fail_native)
    monkeypatch.setattr(engine, "_call_llm", fake_text)

    result = engine.run("修复这个 bug", AgentContext())
    assert result.startswith("文本协议成功")
    assert text_calls["n"] >= 1
    assert engine._native_request_failed is True
    # 熔断后 _call_llm_native 直接走文本协议（不再触碰 native 请求）
    assert engine._call_llm_native(
        [{"role": "user", "content": "x"}],
        [{"type": "function", "function": {"name": "f"}}],
        None,
    ) == '{"thought": "t", "final_answer": "文本协议成功"}'


def test_native_non_provider_error_still_propagates(monkeypatch):
    """非「native provider request failed」的 RuntimeError 仍应冒泡。"""
    engine = ReActEngine(
        ["deepseek/deepseek-v4-pro"],
        max_iterations=2,
        native_fc=True,
    )

    def fail_native(messages, tools, response_format, max_tokens=None):
        raise RuntimeError("模型 auth 失败 (401)")

    monkeypatch.setattr(engine, "_call_with_tools_once", fail_native)
    with pytest.raises(RuntimeError, match="auth 失败"):
        engine._call_llm_native(
            [{"role": "user", "content": "x"}],
            [{"type": "function", "function": {"name": "f"}}],
            None,
        )
