"""Chaos Test 7: 空 tool_call 列表 / finish 但无 tool_call。

目标：验证 ReAct 引擎在 LLM 直接返回 final_answer（无 tool_call）时的
行为——尤其是当任务**需要工具**时不能静默接受（v0.2.x 已知路径）。
"""
from __future__ import annotations


import xenon.engine.base as engine_base
import xenon.utils.llm_client as llm_client
from xenon.engine.react_engine import ReActEngine
from xenon.engine.callbacks import SilentCallback


def test_finish_without_tool_when_tools_required(monkeypatch):
    """任务需要工具（关键字含"读取"）但 LLM 直接返回 final_answer → 引擎拒绝接受。"""
    responses = [
        # 第 1 轮：直接 final_answer（无 tool_call）
        '{"thought": "skip tool", "final_answer": "done (without tools)"}',
        # 第 2 轮：仍然 final_answer
        '{"thought": "still no tool", "final_answer": "done again"}',
        # 第 3 轮：同上
        '{"thought": "really", "final_answer": "really done"}',
        # 第 4 轮：同上
        '{"thought": "fine", "final_answer": "ok final"}',
    ]

    def fake_engine(model_id, messages, **kw):
        return responses.pop(0) if responses else '{"final_answer": "fallback"}'

    def fake_util(model_id, messages, **kw):
        return fake_engine(model_id, messages, **kw)

    def fake_util_stream(model_id, messages, **kw):
        yield fake_engine(model_id, messages, **kw)

    monkeypatch.setattr(engine_base, "chat_completion", fake_engine)
    monkeypatch.setattr(llm_client, "chat_completion", fake_util)
    monkeypatch.setattr(llm_client, "chat_completion_stream", fake_util_stream)

    callback = SilentCallback()
    eng = ReActEngine(
        ["openai/gpt-4o"],
        max_iterations=5,
        callback=callback,
    )
    # "读取" 是 _input_requires_tools 触发的中文关键词
    answer = eng.run("读取 /tmp/x.py 文件内容")
    # 引擎不应静默接受空 tool 的 final_answer；要么要求重试（warning），
    # 要么带警告返回
    warnings = [e for e in callback.events if e[0] == "warning"]
    # 至少有一次 warning（"未执行工具就声称完成"）
    assert any("未执行" in str(e) or "工具" in str(e) for e in warnings), (
        f"任务需要工具时，LLM 直接 final_answer 应触发 warning，实际: {callback.events}"
    )
    # 最终答案应含 warning 提示
    assert "警告" in answer or "未执行" in answer


def test_finish_without_tool_when_not_required(monkeypatch):
    """任务**不需要**工具（如闲聊），LLM 直接 final_answer → 引擎直接接受。"""
    responses = [
        '{"thought": "no tool needed", "final_answer": "answer"}',
    ]

    def fake_engine(model_id, messages, **kw):
        return responses.pop(0) if responses else '{"final_answer": "fallback"}'

    def fake_util(model_id, messages, **kw):
        return fake_engine(model_id, messages, **kw)

    def fake_util_stream(model_id, messages, **kw):
        yield fake_engine(model_id, messages, **kw)

    monkeypatch.setattr(engine_base, "chat_completion", fake_engine)
    monkeypatch.setattr(llm_client, "chat_completion", fake_util)
    monkeypatch.setattr(llm_client, "chat_completion_stream", fake_util_stream)

    callback = SilentCallback()
    eng = ReActEngine(
        ["openai/gpt-4o"],
        max_iterations=5,
        callback=callback,
    )
    answer = eng.run("你好")
    assert answer == "answer"
    # 无 "未执行工具" 警告
    assert not any("未执行" in str(e) for e in callback.events)
