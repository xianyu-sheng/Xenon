"""Final-answer completeness and unbounded display regressions."""

from __future__ import annotations

import json

from xenon.engine.react_engine import ReActEngine, _answer_looks_incomplete
from xenon.repl.context_manager import ContextManager
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL


def test_detects_screenshot_style_mid_sentence_answer():
    answer = (
        "太好了，我找到你电脑上的 dsh 了。接下来用真实文件学习架构。\n\n"
        "## 第 1 课：核心思想\n\n"
        "### 1.1 你启动一个 dsh，其实是"
    )

    assert _answer_looks_incomplete(answer) is True


def test_complete_or_short_answers_are_not_rejected():
    assert _answer_looks_incomplete("好的") is False
    assert _answer_looks_incomplete("这是经过工具验证的完整架构说明。" * 10) is False
    assert _answer_looks_incomplete("本机配置和源码证据都表明这个设计是可靠的") is False
    assert (
        _answer_looks_incomplete("上述工具输出已经核对，按这个方案修改是可以的")
        is False
    )


def test_react_rewrites_abrupt_final_answer_instead_of_displaying_it(monkeypatch):
    partial = (
        "已读取本机配置并确认插件清单。\n\n## 第一课\n\n### 1.1 你启动一个 dsh，其实是"
    )
    complete = (
        "已读取本机配置并确认插件清单。\n\n"
        "## 第一课\n\n"
        "dsh 启动时会依次装配基础 bundle、profile patch 与运行时插件。"
        "你先掌握分层组装、依赖注入和插件生命周期这三个架构思想即可。"
    )
    responses = iter(
        [
            json.dumps({"final_answer": partial}, ensure_ascii=False),
            json.dumps({"final_answer": complete}, ensure_ascii=False),
        ]
    )
    seen_messages = []
    engine = ReActEngine(
        ["test/model"],
        max_iterations=4,
        native_fc=False,
        verification_loop=False,
    )

    def fake_call(phase, messages, *args, **kwargs):
        seen_messages.append(list(messages))
        return next(responses)

    monkeypatch.setattr(engine, "_call_llm_for_phase", fake_call)

    result = engine.run("解释 dsh 的架构思想")

    assert result == complete
    assert partial not in result
    assert len(seen_messages) == 2
    assert "完整重写" in seen_messages[1][-1]["content"]


def test_subagent_formatter_preserves_answer_without_character_cap():
    engine = ReActEngine(["test/model"], native_fc=False)
    answer = "来源明确的长答案。" * 2000

    result = engine._format_sub_result(
        "sub-1",
        "架构分析",
        "react",
        answer,
        object(),
        None,
    )

    assert answer in result
    assert "截断，共" not in result


def test_direct_blocking_uses_complete_model_request_config(monkeypatch):
    registry = ModelRegistry()
    registry.add_model(
        "openai/deepseek-v4-pro",
        "pro",
        api_key="sk-private",
        base_url="https://relay.example/v1",
        max_tokens=256000,
        temperature=0.25,
        reasoning_effort="max",
    )
    captured = {}

    def fake_chat(model_id, messages, **kwargs):
        captured.update(kwargs)
        return "完整回答。"

    monkeypatch.setattr("xenon.utils.llm_client.chat_completion", fake_chat)
    repl = REPL(registry=registry, ctx_mgr=ContextManager(), streaming=False)

    assert (
        repl._blocking_response(
            "openai/deepseek-v4-pro",
            [{"role": "user", "content": "解释架构"}],
        )
        == "完整回答。"
    )
    assert captured["max_tokens"] == 256000
    assert captured["temperature"] == 0.25
    assert captured["credentials"] == {"openai": "sk-private"}
    assert captured["base_url"] == "https://relay.example/v1"
    assert captured["reasoning_effort"] == "max"


def test_direct_streaming_uses_complete_model_request_config(monkeypatch):
    registry = ModelRegistry()
    registry.add_model(
        "openai/deepseek-v4-pro",
        "pro",
        api_key="sk-private",
        base_url="https://relay.example/v1",
        max_tokens=256000,
        temperature=0.25,
        reasoning_effort="max",
    )
    captured = {}

    def fake_stream(model_id, messages, **kwargs):
        captured.update(kwargs)
        yield "完整"
        yield "回答。"

    monkeypatch.setattr("xenon.utils.llm_client.chat_completion_stream", fake_stream)
    repl = REPL(registry=registry, ctx_mgr=ContextManager(), streaming=True)

    assert (
        repl._stream_response(
            "openai/deepseek-v4-pro",
            [{"role": "user", "content": "解释架构"}],
        )
        == "完整回答。"
    )
    assert captured["max_tokens"] == 256000
    assert captured["temperature"] == 0.25
    assert captured["credentials"] == {"openai": "sk-private"}
    assert captured["base_url"] == "https://relay.example/v1"
    assert captured["reasoning_effort"] == "max"
