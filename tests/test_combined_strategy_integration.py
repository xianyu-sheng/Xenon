"""Test strategy advice integration in combined engines."""
from __future__ import annotations

import json

from xenon.engine.callbacks import SilentCallback
from xenon.engine.combined_engines import (
    PlanReactEngine,
    PlanReflectionEngine,
    ReactReflectionEngine,
)
from xenon.engine.context import AgentContext


def test_plan_react_shows_strategy_tip(monkeypatch):
    """PlanReactEngine should display strategy tip for recognized tasks."""
    callback = SilentCallback()
    engine = PlanReactEngine(["test/model"], callback=callback, max_steps=2, react_iterations=1)

    def fake_plan(phase, messages, **kwargs):
        return json.dumps({
            "analysis": "test plan",
            "steps": [
                {"id": 1, "task": "read file", "tool": "read_file", "params": {"file_path": "test.py"}, "depends_on": []}
            ]
        })

    def fake_react(messages, **kwargs):
        return json.dumps({"thought": "done", "final_answer": "ok"})

    monkeypatch.setattr(engine.planner, "_call_llm_for_phase", fake_plan)
    monkeypatch.setattr(engine.reactor, "_call_llm", fake_react)

    ctx = AgentContext()
    engine.run("修复 demo.py 的 bug，并运行测试验证", ctx)

    # Check that strategy tip was emitted
    tips = [value for kind, value in callback.events if kind == "tip"]
    assert len(tips) == 1
    assert "调试任务" in tips[0]
    assert ctx.get("_strategy_tip_emitted") is True


def test_plan_react_chat_does_not_show_strategy(monkeypatch):
    """PlanReactEngine should not show strategy for chat-only tasks."""
    callback = SilentCallback()
    engine = PlanReactEngine(["test/model"], callback=callback, max_steps=1, react_iterations=1)

    def fake_plan(phase, messages, **kwargs):
        return json.dumps({
            "analysis": "chat response",
            "steps": []
        })

    monkeypatch.setattr(engine.planner, "_call_llm_for_phase", fake_plan)

    engine.run("你好", AgentContext())

    tips = [value for kind, value in callback.events if kind == "tip"]
    assert len(tips) == 0


def test_plan_reflection_shows_strategy_tip(monkeypatch):
    """PlanReflectionEngine should display strategy tip."""
    callback = SilentCallback()
    engine = PlanReflectionEngine(
        ["test/model"],
        callback=callback,
        max_steps=1,
        review_rounds=1,
        pass_threshold=5
    )

    def fake_plan(messages, **kwargs):
        return json.dumps({"thought": "done", "final_answer": "implemented"})

    def fake_review(phase, messages, **kwargs):
        return json.dumps({"score": 8, "passed": True, "feedback": "good"})

    monkeypatch.setattr(engine.planner, "_call_llm", fake_plan)
    monkeypatch.setattr(engine.reflector, "_call_llm_for_phase", fake_review)

    ctx = AgentContext()
    engine.run("重构 UserService 为 AccountService", ctx)

    tips = [value for kind, value in callback.events if kind == "tip"]
    assert len(tips) == 1
    assert "重构任务" in tips[0]


def test_react_reflection_shows_strategy_tip(monkeypatch):
    """ReactReflectionEngine should display strategy tip."""
    callback = SilentCallback()
    engine = ReactReflectionEngine(
        ["test/model"],
        callback=callback,
        react_iterations=1,
        review_rounds=1,
        pass_threshold=5
    )

    def fake_react(messages, **kwargs):
        return json.dumps({"thought": "done", "final_answer": "code written"})

    def fake_review(phase, messages, **kwargs):
        return json.dumps({"score": 9, "passed": True, "feedback": "excellent"})

    monkeypatch.setattr(engine.reactor, "_call_llm", fake_react)
    monkeypatch.setattr(engine.reflector, "_call_llm_for_phase", fake_review)

    ctx = AgentContext()
    engine.run("编写快速排序算法并添加测试", ctx)

    tips = [value for kind, value in callback.events if kind == "tip"]
    assert len(tips) == 1
    # Should match either write_test or write_code
    assert any(keyword in tips[0] for keyword in ["测试任务", "实现任务"])


def test_combined_engine_respects_tip_deduplication(monkeypatch):
    """Combined engines should not emit duplicate tips for same context."""
    callback = SilentCallback()
    engine = PlanReactEngine(["test/model"], callback=callback, max_steps=1, react_iterations=1)

    def fake_plan(phase, messages, **kwargs):
        return json.dumps({
            "analysis": "test",
            "steps": [{"id": 1, "task": "test", "tool": None, "params": {}, "depends_on": []}]
        })

    def fake_react(messages, **kwargs):
        return json.dumps({"thought": "done", "final_answer": "ok"})

    monkeypatch.setattr(engine.planner, "_call_llm_for_phase", fake_plan)
    monkeypatch.setattr(engine.reactor, "_call_llm", fake_react)

    ctx = AgentContext()
    # First run
    engine.run("修复 bug1.py", ctx)

    # Second run with NEW context (separate top-level task)
    ctx2 = AgentContext()
    engine.run("修复 bug2.py", ctx2)

    tips = [value for kind, value in callback.events if kind == "tip"]
    # Should show tip twice because these are separate contexts (separate tasks)
    assert len(tips) == 2
