"""Research tasks converge after repeated external-tool failures."""

from __future__ import annotations

from xenon.engine.callbacks import SilentCallback
from xenon.engine.context import AgentContext
from xenon.engine.react_engine import ReActEngine
from xenon.repl.execution_policy import ExecutionLevel


def test_react_stops_exploring_after_three_consecutive_tool_failures(monkeypatch):
    responses = iter(
        [
            '{"action":"web_fetch","action_input":{"url":"https://a.invalid"}}',
            '{"action":"web_fetch","action_input":{"url":"https://b.invalid"}}',
            '{"action":"web_fetch","action_input":{"url":"https://c.invalid"}}',
            "现有外部来源均不可访问，因此只能报告证据不足，建议稍后重试。",
        ]
    )
    callback = SilentCallback()
    engine = ReActEngine(
        ["test/model"],
        native_fc=False,
        callback=callback,
        max_iterations=10,
    )
    monkeypatch.setattr(engine, "_call_llm", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(
        "xenon.nodes.tool_executor.ToolNode.execute",
        lambda self, context: {
            "success": False,
            "error": "connection unavailable",
            "retryable": False,
        },
    )

    result = engine.run(
        "请调研一下模型厂商 Agent 项目的维护速度",
        AgentContext({"_execution_level": int(ExecutionLevel.READ_ONLY)}),
    )

    assert "证据不足" in result
    assert engine._last_tracker is not None
    assert len(engine._last_tracker.calls) == 3
    assert engine._last_tracker.consecutive_failures() == 3
    warnings = [payload for kind, payload in callback.events if kind == "warning"]
    assert any("连续失败 3 次" in warning for warning in warnings)


def test_write_task_gets_recovery_hint_instead_of_early_stop(monkeypatch):
    """C13: 写/实现任务连续失败 3 次只提示换方法，不直接 mercy 收束。"""
    responses = iter(
        [
            '{"action":"read_file","action_input":{"file_path":"a.py"}}',
            '{"action":"read_file","action_input":{"file_path":"b.py"}}',
            '{"action":"read_file","action_input":{"file_path":"c.py"}}',
            '{"thought":"已完成","final_answer":"已完成"}',
        ]
    )
    callback = SilentCallback()
    engine = ReActEngine(
        ["test/model"],
        native_fc=False,
        callback=callback,
        max_iterations=10,
    )
    monkeypatch.setattr(engine, "_call_llm", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(
        "xenon.nodes.tool_executor.ToolNode.execute",
        lambda self, context: {
            "success": False,
            "error": "temporary failure",
            "retryable": True,
        },
    )

    result = engine.run("请实现一个新的 CLI 功能")

    assert "已完成" in result
    assert engine._last_tracker is not None
    assert len(engine._last_tracker.calls) == 3
    warnings = [payload for kind, payload in callback.events if kind == "warning"]
    assert any("恢复提示" in warning for warning in warnings)
    assert not any("停止继续探索" in warning for warning in warnings)
