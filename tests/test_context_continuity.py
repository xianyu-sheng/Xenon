"""Regression tests for cross-turn tool traces and working memory."""

from __future__ import annotations

from types import SimpleNamespace

from xenon.engine.combined_engines import PlanReactEngine
from xenon.engine.context import AgentContext
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.react_engine import ReActEngine
from xenon.engine.tool_tracker import ToolExecutionTracker
from xenon.repl.context_manager import ContextManager
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL


def _repl() -> REPL:
    registry = ModelRegistry()
    registry.add_model("openai/a", "a")
    repl = REPL(registry=registry, streaming=False)
    repl.model_pool.register("openai/a", alias="a")
    return repl


def test_tool_trace_is_persisted_redacted_and_api_safe():
    ctx = ContextManager()
    ctx.add_tool_trace(
        "web_fetch",
        {"url": "https://example.test", "api_key": "top-secret"},
        True,
        result="fetched repository metadata",
    )

    assert [turn.role for turn in ctx.history] == ["assistant", "tool"]
    assert ctx.history[0].turn_type == "tool_call"
    assert "top-secret" not in ctx.history[0].content
    assert "[REDACTED]" in ctx.history[0].content

    messages = ctx.get_messages()
    assert [message["role"] for message in messages] == ["assistant", "user"]
    assert "[工具结果: web_fetch]" in messages[1]["content"]


def test_working_memory_is_bounded_redacted_and_opt_in():
    ctx = ContextManager()
    ctx.update_working_memory("session_created_files", ["/tmp/example.py"])
    ctx.update_working_memory("api_token", "never-send-this")

    assert ctx.get_messages() == []
    messages = ctx.get_messages(include_working_memory=True)

    assert messages[0]["role"] == "system"
    assert "/tmp/example.py" in messages[0]["content"]
    assert "never-send-this" not in messages[0]["content"]
    assert "[REDACTED]" in messages[0]["content"]


def test_react_injects_memory_without_duplicating_current_user():
    ctx = ContextManager()
    ctx.update_working_memory("session_active_dirs", ["/work/project"])
    ctx.add_user_message("继续")
    engine = ReActEngine(["openai/a"], max_iterations=2)
    captured: dict[str, list[dict[str, str]]] = {}

    def fake_llm(messages, max_tokens=None):
        captured["messages"] = messages
        return '{"thought":"done","final_answer":"ok"}'

    engine._call_llm = fake_llm
    engine._input_requires_tools = lambda value: False
    engine._parse_response = lambda value: {
        "thought": "done",
        "final_answer": "ok",
    }

    assert engine.run("继续", AgentContext(), ctx_mgr=ctx) == "ok"
    messages = captured["messages"]
    assert sum(
        message["role"] == "user" and message["content"] == "继续"
        for message in messages
    ) == 1
    assert any("/work/project" in message["content"] for message in messages)


def test_repl_persists_tracker_and_file_memory_once(tmp_path):
    repl = _repl()
    repl.ctx_mgr.add_user_message("创建文件")
    tracker = ToolExecutionTracker()
    target = tmp_path / "hello.py"
    tracker.record(
        "write_file",
        {"file_path": str(target), "content": "print('hello')"},
        True,
        "写入成功",
    )
    engine = SimpleNamespace(_last_tracker=tracker)

    assert repl._persist_engine_trace(engine) == 1
    assert repl._persist_engine_trace(engine) == 0
    assert [turn.role for turn in repl.ctx_mgr.history] == [
        "user", "assistant", "tool",
    ]
    memory = repl.ctx_mgr.get_working_memory()
    assert str(target) in memory["session_created_files"]
    assert memory["recent_tool_activity"][-1]["tool"] == "write_file"


def test_plan_execute_exposes_its_verified_tracker(monkeypatch):
    engine = PlanExecuteEngine(["openai/a"])
    monkeypatch.setattr(
        engine,
        "_plan",
        lambda user_input, context=None: {
            "analysis": "one step",
            "steps": [{"id": 1, "task": "inspect", "tool": "read_file"}],
        },
    )

    def execute(tool, params, context, tracker=None):
        tracker.record(tool, params, True, "read ok")
        return "read ok"

    monkeypatch.setattr(engine, "_execute_step_with_tool", execute)
    monkeypatch.setattr(engine, "_summarize", lambda *args: "done")

    assert engine.run("inspect") == "done"
    assert engine._last_tracker is not None
    assert engine._last_tracker.successful_tools() == ["read_file"]


def test_plan_react_aggregates_tool_traces_from_every_step(monkeypatch):
    engine = PlanReactEngine(["openai/a"], max_steps=2)
    monkeypatch.setattr(
        engine.planner,
        "_plan",
        lambda user_input, context=None: {
            "analysis": "two steps",
            "steps": [
                {"id": 1, "task": "first"},
                {"id": 2, "task": "second"},
            ],
        },
    )
    counter = {"value": 0}

    def reactor_run(user_input, context=None, ctx_mgr=None):
        counter["value"] += 1
        tracker = ToolExecutionTracker()
        tracker.record(
            "read_file",
            {"file_path": f"file-{counter['value']}.py"},
            True,
            "ok",
        )
        engine.reactor._last_tracker = tracker
        return "ok"

    monkeypatch.setattr(engine.reactor, "run", reactor_run)

    engine.run("inspect both")
    assert engine._last_tracker is not None
    assert len(engine._last_tracker.calls) == 2


def test_runtime_checkpoint_preserves_tool_roles_and_memory(monkeypatch, tmp_path):
    import xenon.repl.session as session_module

    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    repl = _repl()
    repl.ctx_mgr.add_user_message("inspect")
    repl.ctx_mgr.add_tool_trace("read_file", {"file_path": "README.md"}, True, "ok")
    repl.ctx_mgr.update_working_memory("session_active_dirs", [str(tmp_path)])

    repl._auto_save_session()
    saved = session_module.load_session("_auto")

    assert [item["role"] for item in saved["history"]] == [
        "user", "assistant", "tool",
    ]
    assert saved["history"][-1]["turn_type"] == "tool_result"
    assert saved["extra"]["working_memory"]["session_active_dirs"] == [
        str(tmp_path)
    ]
