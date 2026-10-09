"""D2 v1：子代理事实记录 + SubagentStop hook。"""

from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.engine.react_engine import ReActEngine
from xenon.hooks.runner import HookOutcome


def test_record_fact_uses_registered_callback():
    ctx = AgentContext()
    facts: list = []
    ctx.set_fact_callback(lambda event, **data: facts.append((event, data)))
    ctx.record_fact("subagent/start", task="t", engine="react")
    assert facts == [("subagent/start", {"task": "t", "engine": "react"})]


def test_record_fact_is_silent_without_callback():
    AgentContext().record_fact("x", a=1)  # 不抛异常


def test_run_hook_returns_outcome(monkeypatch):
    ctx = AgentContext()
    ctx.set_hook_callback(
        lambda event, tool, tool_input: HookOutcome(stop=True, message="停")
    )
    outcome = ctx.run_hook("SubagentStop", "spawn_agent", {"task": "t"})
    assert outcome.stop is True


class _FakeSubEngine:
    def run(self, task, ctx):
        return "子任务完成"


def test_spawn_subagent_records_facts_and_hook_note(monkeypatch):
    eng = ReActEngine(["m1"])
    eng._format_sub_result = lambda *a, **k: "✅ 子任务完成"
    monkeypatch.setattr(eng, "_build_sub_engine", lambda engine_type, task_id: _FakeSubEngine())

    ctx = AgentContext()
    facts: list = []
    ctx.set_fact_callback(lambda event, **data: facts.append((event, data)))
    ctx.set_hook_callback(
        lambda event, tool, tool_input: HookOutcome(stop=True)
    )

    out = eng._spawn_subagent({"task": "总结模块"}, ctx, None)

    assert out.startswith("✅")
    assert "hook 请求停止" in out
    kinds = [f[0] for f in facts]
    assert kinds == ["subagent/start", "subagent/result"]
    assert facts[1][1]["success"] is True


def test_subagent_read_only_by_default(monkeypatch):
    """决策 4：子代理默认只读——父级 EXECUTE 权限也要封顶到 READ_ONLY。"""

    from xenon.engine.context import AgentContext
    from xenon.repl.execution_policy import ExecutionLevel

    eng = ReActEngine(["m1"])
    captured: dict = {}

    class _CaptureSub:
        def run(self, task, ctx):
            captured["level"] = ctx.get("_execution_level")
            captured["reason"] = ctx.get("_execution_reason")
            return "子任务完成"

    monkeypatch.setattr(eng, "_build_sub_engine", lambda engine_type, task_id: _CaptureSub())
    monkeypatch.setattr(eng, "_format_sub_result", lambda *a, **k: "✅ 结果")

    ctx = AgentContext()
    ctx.set("_execution_level", int(ExecutionLevel.EXECUTE))
    ctx.set_conversation_messages([])
    eng._subagent_depth = 0
    eng._subagent_history = []

    eng._spawn_subagent({"task": "总结模块"}, ctx, None)

    assert captured["level"] == int(ExecutionLevel.READ_ONLY)
    assert "只读" in captured["reason"]


def test_subagent_read_only_can_be_lifted_explicitly(monkeypatch):
    """父级显式 read_only=false → 继承父级级别（不提升）。"""

    from xenon.engine.context import AgentContext
    from xenon.repl.execution_policy import ExecutionLevel

    eng = ReActEngine(["m1"])
    captured: dict = {}

    class _CaptureSub:
        def run(self, task, ctx):
            captured["level"] = ctx.get("_execution_level")
            return "子任务完成"

    monkeypatch.setattr(eng, "_build_sub_engine", lambda engine_type, task_id: _CaptureSub())
    monkeypatch.setattr(eng, "_format_sub_result", lambda *a, **k: "✅ 结果")

    ctx = AgentContext()
    ctx.set("_execution_level", int(ExecutionLevel.EXECUTE))
    ctx.set_conversation_messages([])
    eng._subagent_depth = 0
    eng._subagent_history = []

    eng._spawn_subagent({"task": "写文件", "read_only": False}, ctx, None)

    assert captured["level"] == int(ExecutionLevel.EXECUTE)


def test_parallel_subagents_default_read_only(monkeypatch):
    from xenon.engine.context import AgentContext
    from xenon.repl.execution_policy import ExecutionLevel

    eng = ReActEngine(["m1"])
    captured: dict = {}

    class _CaptureSub:
        def run(self, task, ctx):
            captured["level"] = ctx.get("_execution_level")
            return "子任务完成"

    monkeypatch.setattr(eng, "_build_sub_engine", lambda engine_type, task_id: _CaptureSub())
    monkeypatch.setattr(eng, "_format_sub_result", lambda *a, **k: "✅ 结果")

    ctx = AgentContext()
    ctx.set("_execution_level", int(ExecutionLevel.EXECUTE))
    ctx.set_conversation_messages([])
    eng._subagent_depth = 0
    eng._subagent_history = []

    eng._spawn_all_subagents([{"task": "并行任务"}], ctx, None)

    assert captured["level"] == int(ExecutionLevel.READ_ONLY)
