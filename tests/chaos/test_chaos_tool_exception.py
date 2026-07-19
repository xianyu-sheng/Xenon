"""Chaos Test 4: 工具执行失败。

目标：验证 ``ToolExecutor.execute`` 在 tool 抛异常 / 返回失败时：
1. 不挂死（不无限重试）；
2. 不会让 REPL 进入死循环；
3. 失败信息传递到 ReAct 引擎后能被识别为 Observation（继续推进）。
"""
from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.engine.tool_tracker import ToolExecutionTracker
from xenon.nodes import tool_executor as te_mod
from xenon.nodes.tool_executor import ToolExecutor


class _BoomNode:
    """模拟 tool 执行抛异常。"""

    def __init__(self, name, action_type=None, **params):
        self.action_type = action_type

    @staticmethod
    def normalize_params(p):
        return p

    def execute(self, context):
        raise RuntimeError("boom! tool crashed")


class _ReturnFailureNode:
    """模拟 tool 返回 success=False。"""

    def __init__(self, name, action_type=None, **params):
        self.action_type = action_type

    @staticmethod
    def normalize_params(p):
        return p

    def execute(self, context):
        return {"success": False, "error": "permission denied"}


def _executor_with_node(monkeypatch, node_cls):
    monkeypatch.setattr(te_mod, "ToolNode", node_cls)
    return ToolExecutor(retry_attempts=2)


def test_tool_exception_does_not_hang(monkeypatch):
    """tool 抛 RuntimeError → ToolExecutor 不挂死，返回失败结果。"""
    ex = _executor_with_node(monkeypatch, _BoomNode)
    tracker = ToolExecutionTracker()
    r = ex.execute(
        "read_file", {"file_path": "/x"}, AgentContext(),
        tracker=tracker, tools={"read_file": {}},
    )
    assert r.success is False
    assert "boom" in r.error.lower() or "runtimeerror" in r.error.lower()
    # tracker 记录了失败
    assert len(tracker.calls) == 1
    assert tracker.calls[0].success is False


def test_tool_returning_failure_does_not_retry_terminal(monkeypatch):
    """tool 返回 success=False 且错误是 terminal（permission denied）→ 不重试。"""
    ex = _executor_with_node(monkeypatch, _ReturnFailureNode)
    r = ex.execute(
        "read_file", {"file_path": "/x"}, AgentContext(),
        tools={"read_file": {}},
    )
    assert r.success is False
    # 终端错误（permission denied）不重试 → attempts 应为 1
    assert r.attempts == 1


def test_tool_exception_retry_capped(monkeypatch):
    """tool 持续抛异常 → 重试次数受 retry_attempts 限制，不无限循环。"""
    ex = _executor_with_node(monkeypatch, _BoomNode)
    r = ex.execute(
        "command", {"action": "rm -rf /"}, AgentContext(),
        tools={"command": {}},
    )
    assert r.success is False
    # retry_attempts=2 → 最多 2 次尝试
    assert r.attempts <= 2


def test_tool_failure_observes_through_react(monkeypatch):
    """端到端验证：tool 失败后，ReAct 引擎的 Observation 含可读错误。"""
    import xenon.engine.base as engine_base
    import xenon.utils.llm_client as llm_client
    from xenon.engine.react_engine import ReActEngine
    from xenon.engine.callbacks import SilentCallback

    # LLM 第一次返回 tool_call，第二次看到失败 Observation 后返回 final_answer
    responses = [
        # 1. 触发 tool 失败
        '{"thought": "try to read", "action": "read_file", "action_input": {"file_path": "/x"}}',
        # 2. 看到 Observation 失败后，承认无法完成
        '{"thought": "tool failed, give up", "final_answer": "工具执行失败: boom"}',
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
    monkeypatch.setattr(te_mod, "ToolNode", _BoomNode)

    eng = ReActEngine(["openai/gpt-4o"], max_iterations=5, callback=SilentCallback())
    answer = eng.run("read /x")
    assert "失败" in answer or "boom" in answer
    # 引擎没挂死
    assert isinstance(answer, str)
