"""Regression tests for cross-turn tool traces and working memory."""

from __future__ import annotations

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


def test_replaceable_context_does_not_accumulate_in_history():
    ctx = ContextManager()
    ctx.set_context_message("long_term_memory", "first memory")
    ctx.set_context_message("long_term_memory", "replacement memory")

    assert ctx.history == []
    assert ctx.get_messages() == []
    messages = ctx.get_messages(include_context_messages=True)
    assert len(messages) == 1
    assert messages[0]["content"] == "replacement memory"


def test_cache_tiers_keep_volatile_context_after_reusable_history():
    ctx = ContextManager()
    ctx.add_system_message("fixed system")
    ctx.set_context_message("project", "stable project", stable=True)
    ctx.add_user_message("first question")
    ctx.add_assistant_message("first answer")
    ctx.update_working_memory("active_file", "/work/main.py")
    ctx.set_context_message("long_term_memory", "volatile retrieval")
    ctx.add_user_message("current question")

    messages = ctx.get_messages(
        include_working_memory=True,
        include_context_messages=True,
    )
    contents = [message["content"] for message in messages]

    assert contents[0:4] == [
        "fixed system",
        "stable project",
        "first question",
        "first answer",
    ]
    assert "active_file" in contents[4]
    assert contents[5:] == ["volatile retrieval", "current question"]


def test_repl_binds_turn_hint_to_user_prompt_not_system_overlay(monkeypatch):
    repl = _repl()
    repl.registry.assign_role("planner", ["a"])
    monkeypatch.setattr(repl.auto_router, "is_empty", lambda: True)
    monkeypatch.setattr(repl, "_inject_project_context", lambda: None)
    monkeypatch.setattr(repl, "_inject_memories", lambda value: None)
    monkeypatch.setattr(repl, "_commit_memory_usage", lambda: None)
    monkeypatch.setattr(repl, "_maybe_suggest_memory", lambda value: None)
    captured: list[str] = []
    monkeypatch.setattr(
        repl,
        "_run_direct",
        lambda prompt, model_ids, intent=None, execution_policy=None: captured.append(
            prompt
        ),
    )

    repl._handle_chat("帮我写一个快速排序算法")

    assert captured
    assert "## 本轮回答指导" in captured[0]
    assert "高级编程专家" in captured[0]
    assert all(
        "高级编程专家" not in message["content"]
        for message in repl.ctx_mgr.get_context_messages()
    )
    assert repl.ctx_mgr.history[-1].role == "user"
    assert repl.ctx_mgr.history[-1].content == captured[0]


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
        "user",
        "assistant",
        "tool",
    ]
    assert saved["history"][-1]["turn_type"] == "tool_result"
    assert saved["extra"]["working_memory"]["session_active_dirs"] == [str(tmp_path)]


# The three assertions about "the engine injects memory into its messages"
# were removed with the engine: the kernel has no such injection yet, and the
# gap is tracked in #340 rather than silently forgotten.
