"""P0-2：规划优先走原生 ``submit_plan`` 工具调用。

与 Claude Code / Codex 对齐：计划由 provider 校验过的结构化参数提交，
而不是让模型手写 13KB JSON。文本 JSON 仍是兼容端点/测试的降级路径。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from xenon.engine.context import AgentContext
from xenon.engine.plan_execute_engine import PlanExecuteEngine

PLAN_PAYLOAD = {
    "analysis": "创建并验证文件",
    "steps": [
        {
            "id": 1,
            "task": "创建 note.txt",
            "tool": "write_file",
            "params": {"file_path": "note.txt", "content": "hello"},
            "depends_on": [],
        },
        {
            "id": 2,
            "task": "确认结果",
            "tool": "",
            "params": {},
            "depends_on": [1],
        },
    ],
}


def _native_react_json() -> str:
    return json.dumps(
        {"thought": "", "action": "submit_plan", "action_input": PLAN_PAYLOAD},
        ensure_ascii=False,
    )


def _engine(*, native_planning: bool = True) -> PlanExecuteEngine:
    return PlanExecuteEngine(["fake/model"], native_planning=native_planning)


def test_plan_uses_native_submit_plan(monkeypatch) -> None:
    engine = _engine()
    calls: list[dict] = []

    def fake_native(phase, messages, **kwargs):
        calls.append(kwargs)
        return _native_react_json()

    monkeypatch.setattr(engine, "_call_llm_native_for_phase", fake_native)
    monkeypatch.setattr(
        engine,
        "_call_llm_for_phase",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("text path not expected")),
    )

    plan = engine._plan("创建 note.txt", AgentContext())

    assert [s["task"] for s in plan["steps"]] == ["创建 note.txt", "确认结果"]
    assert plan["steps"][0]["tool"] == "write_file"
    assert plan["steps"][1]["tool"] is None  # 原生 schema 用空串表示无工具
    assert calls and calls[0]["tool_choice"]["function"]["name"] == "submit_plan"
    assert calls[0]["tools_schema"][0]["function"]["name"] == "submit_plan"
    assert engine._pending_native_response is None


def test_plan_falls_back_to_text_json(monkeypatch) -> None:
    engine = _engine()
    text_plan = json.dumps(PLAN_PAYLOAD, ensure_ascii=False)
    calls: list[str] = []

    monkeypatch.setattr(
        engine, "_call_llm_native_for_phase", lambda *a, **k: ""
    )

    def fake_text(phase, messages, **kwargs):
        calls.append(phase)
        return text_plan

    monkeypatch.setattr(engine, "_call_llm_for_phase", fake_text)

    plan = engine._plan("创建 note.txt", AgentContext())

    assert len(plan["steps"]) == 2
    assert calls == ["plan"]  # 原生失败后的一次文本恢复


def test_plan_native_exception_keeps_text_path(monkeypatch) -> None:
    engine = _engine()

    def boom(*args, **kwargs):
        raise RuntimeError("provider does not support tools")

    monkeypatch.setattr(engine, "_call_llm_native_for_phase", boom)
    monkeypatch.setattr(
        engine,
        "_call_llm_for_phase",
        lambda *a, **k: json.dumps(PLAN_PAYLOAD, ensure_ascii=False),
    )

    plan = engine._plan("创建 note.txt", AgentContext())

    assert len(plan["steps"]) == 2


def test_native_tool_choice_is_passed_to_provider(monkeypatch) -> None:
    engine = _engine(native_planning=False)
    captured: dict = {}

    def fake_once(messages, tools, response_format, max_tokens=None, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(has_tool_calls=False, content='{"ok":1}')

    monkeypatch.setattr(engine, "_call_with_tools_once", fake_once)

    out = engine._call_llm_native(
        [{"role": "user", "content": "hi"}],
        tools_schema=[{"type": "function", "function": {"name": "submit_plan"}}],
        tool_choice={"type": "function", "function": {"name": "submit_plan"}},
    )

    assert out == '{"ok":1}'
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_plan"},
    }
