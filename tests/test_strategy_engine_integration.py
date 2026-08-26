"""Engine integration tests for task-local strategy advice."""

from __future__ import annotations

import json

from xenon.engine.callbacks import SilentCallback
from xenon.engine.context import AgentContext
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.react_engine import ReActEngine
from xenon.utils.deepseek_cache import _hash_system_prompt


def test_react_injects_debug_strategy_into_current_user_turn(monkeypatch):
    callback = SilentCallback()
    engine = ReActEngine(["test/model"], callback=callback, max_iterations=1)
    captured = []

    def fake_call(messages, **kwargs):
        captured.extend(messages)
        return json.dumps({"thought": "done", "final_answer": "ok"})

    monkeypatch.setattr(engine, "_call_llm", fake_call)
    engine.run("修复 demo.py 的 bug，并运行测试验证")

    assert "[Xenon 本轮策略]" in captured[-1]["content"]
    assert "调试任务" in captured[-1]["content"]
    assert "复杂度提示" in captured[-1]["content"]
    assert any(kind == "tip" and "调试任务" in value for kind, value in callback.events)


def test_react_chat_does_not_inject_strategy(monkeypatch):
    engine = ReActEngine(["test/model"], callback=SilentCallback(), max_iterations=1)
    captured = []

    def fake_call(messages, **kwargs):
        captured.extend(messages)
        return json.dumps({"thought": "done", "final_answer": "你好"})

    monkeypatch.setattr(engine, "_call_llm", fake_call)
    engine.run("你好")

    assert "[Xenon 本轮策略]" not in captured[-1]["content"]


def test_plan_injects_strategy_only_into_user_message(monkeypatch):
    callback = SilentCallback()
    engine = PlanExecuteEngine(["test/model"], callback=callback)
    captured = []

    def fake_phase(phase, messages, **kwargs):
        captured.extend(messages)
        return '{"analysis":"ok","steps":[{"id":1,"task":"read","tool":"read_file","params":{"file_path":"demo.py"},"depends_on":[]}]}'

    monkeypatch.setattr(engine, "_call_llm_for_phase", fake_phase)
    before = _hash_system_prompt(engine.system_prompt)
    result = engine._plan("修复 demo.py 的 bug", AgentContext())
    after = _hash_system_prompt(engine.system_prompt)

    assert result["steps"]
    assert "[Xenon 本轮策略]" not in captured[0]["content"]
    assert "[Xenon 本轮策略]" in captured[-1]["content"]
    assert before == after
    assert any(kind == "tip" for kind, _ in callback.events)


def test_strategy_tip_marker_is_scoped_to_one_context():
    from xenon.engine.combined_engines import _isolated_ctx

    parent = AgentContext()
    parent.set("_strategy_tip_emitted", True)
    child = _isolated_ctx(parent)
    assert child.get("_strategy_tip_emitted") is True
    assert child.get("_strategy_phase_context") is True


def test_new_top_level_task_resets_tip_marker(monkeypatch):
    callback = SilentCallback()
    context = AgentContext()
    engine = ReActEngine(["test/model"], callback=callback, max_iterations=1)

    monkeypatch.setattr(
        engine,
        "_call_llm",
        lambda messages, **kwargs: json.dumps(
            {"thought": "done", "final_answer": "ok"}
        ),
    )
    engine.run("修复 first.py 的 bug", context)
    engine.run("修复 second.py 的 bug", context)

    tips = [value for kind, value in callback.events if kind == "tip"]
    assert len(tips) == 2
