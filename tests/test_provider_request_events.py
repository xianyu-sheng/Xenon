"""provider_request_start/end 事件接线测试。

ExecutionPolicy.emit() 发出的 provider_request_start/end 事件应通过
event_sink 路由到 EngineCallback.on_provider_request_start/end() 方法，
让库用户能观察 LLM 请求的开始与结束。

这是 v0.8.5 前遗留的小债：事件定义存在但未接线，现在补全。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xenon.engine.callbacks import SilentCallback
from xenon import create_engine


def test_event_sink_is_bound_to_execution_policy():
    """BaseEngine 构造时应把事件路由器绑到 ExecutionPolicy.event_sink。"""
    engine = create_engine("react", ["deepseek/deepseek-chat"])
    assert engine.execution_policy.event_sink is not None


def test_provider_request_events_route_to_callback():
    """ExecutionPolicy.emit() 发出的事件应路由到 EngineCallback 方法。"""
    cb = SilentCallback()
    engine = create_engine("react", ["deepseek/deepseek-chat"], callback=cb)

    # 手动触发事件（模拟 _call_llm 里的 emit 调用）
    engine.execution_policy.emit(
        "provider_request_start",
        phase="llm:test/model",
        timeout=120.0,
        provider_attempts=3,
    )
    engine.execution_policy.emit(
        "provider_request_end",
        phase="llm:test/model",
        success=True,
    )

    # 验证 callback 收到了事件
    event_names = [ev[0] for ev in cb.events]
    assert "provider_request_start" in event_names
    assert "provider_request_end" in event_names

    # 验证 payload 完整传递
    start_event = [ev for ev in cb.events if ev[0] == "provider_request_start"][0]
    assert start_event[1]["phase"] == "llm:test/model"
    assert start_event[1]["timeout"] == 120.0
    assert start_event[1]["provider_attempts"] == 3

    end_event = [ev for ev in cb.events if ev[0] == "provider_request_end"][0]
    assert end_event[1]["phase"] == "llm:test/model"
    assert end_event[1]["success"] is True


def test_unknown_events_are_silently_ignored():
    """未知事件不应导致路由器崩溃（为未来扩展预留空间）。"""
    cb = SilentCallback()
    engine = create_engine("react", ["deepseek/deepseek-chat"], callback=cb)

    # 发出一个路由器不认识的事件
    engine.execution_policy.emit(
        "future_event_from_v0.9.0",
        some_field="value",
    )

    # 不应崩溃，也不应出现在 callback.events 里
    event_names = [ev[0] for ev in cb.events]
    assert "future_event_from_v0.9.0" not in event_names
