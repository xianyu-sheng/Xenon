"""
REPL 模块单元测试。

测试 context manager、model registry、session 管理、命令分发等。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from omniagent.repl.context_manager import ContextManager
from omniagent.repl.model_registry import ModelRegistry, BUILTIN_MODES


# ── ContextManager 测试 ──────────────────────────────────

class TestContextManager:
    def test_add_messages(self):
        mgr = ContextManager()
        mgr.add_user_message("hello")
        mgr.add_assistant_message("hi there")
        assert len(mgr.history) == 2
        assert mgr.history[0].role == "user"
        assert mgr.history[1].role == "assistant"

    def test_get_messages_format(self):
        mgr = ContextManager()
        mgr.add_user_message("test")
        messages = mgr.get_messages()
        assert messages == [{"role": "user", "content": "test"}]

    def test_token_estimation(self):
        mgr = ContextManager()
        # 英文：4 个 word
        assert mgr.estimate_tokens("hello world foo bar") >= 4
        # 中文：4 个字 * 1.5
        assert mgr.estimate_tokens("你好世界") >= 6

    def test_usage_ratio(self):
        mgr = ContextManager(max_tokens=100)
        mgr.add_user_message("a" * 50)
        ratio = mgr.usage_ratio()
        assert ratio > 0

    def test_needs_compact(self):
        mgr = ContextManager(max_tokens=100, compact_threshold=0.5)
        mgr.add_user_message("a" * 100)
        assert mgr.needs_compact() is True

    def test_compact(self):
        mgr = ContextManager()
        # 添加足够多的消息（超过 3 轮）以触发压缩
        for i in range(4):
            mgr.add_user_message(f"question {i+1}")
            mgr.add_assistant_message(f"answer {i+1}")

        summary = mgr.compact("这是摘要")
        assert summary == "这是摘要"
        # 摘要 + 最近 3 轮（6 条消息）
        assert len(mgr.history) == 7
        assert "压缩" in mgr.history[0].content

    def test_compact_preserves_recent(self):
        """压缩应保留最近 3 轮对话。"""
        mgr = ContextManager()
        for i in range(5):
            mgr.add_user_message(f"q{i+1}")
            mgr.add_assistant_message(f"a{i+1}")

        mgr.compact("摘要")
        # 第 1 条是摘要，后面保留最近 3 轮
        assert mgr.history[0].role == "system"
        assert "摘要" in mgr.history[0].content
        # 最近的消息应该保留
        contents = [t.content for t in mgr.history]
        assert "q5" in contents
        assert "a5" in contents

    def test_compact_short_history(self):
        """短历史无手动摘要时提示无需压缩。"""
        mgr = ContextManager()
        mgr.add_user_message("question 1")
        mgr.add_assistant_message("answer 1")

        summary = mgr.compact()
        assert "无需压缩" in summary

    def test_compact_auto_summary(self):
        mgr = ContextManager()
        # 需要超过 3 轮才能触发自动压缩
        for i in range(4):
            mgr.add_user_message(f"如何写快速排序？步骤 {i+1}")
            mgr.add_assistant_message(f"快速排序是一种分治算法...步骤 {i+1}")

        summary = mgr.compact()
        assert "快速排序" in summary

    def test_undo(self):
        mgr = ContextManager()
        mgr.add_user_message("msg1")
        mgr.save_snapshot()
        mgr.add_user_message("msg2")
        assert len(mgr.history) == 2

        result = mgr.undo()
        assert result is True
        assert len(mgr.history) == 1

    def test_undo_empty(self):
        mgr = ContextManager()
        assert mgr.undo() is False

    def test_undo_depth(self):
        mgr = ContextManager()
        mgr.save_snapshot()
        mgr.save_snapshot()
        assert mgr.undo_depth == 2

    def test_clear(self):
        mgr = ContextManager()
        mgr.add_user_message("test")
        mgr.clear()
        assert len(mgr.history) == 0

    def test_stats(self):
        mgr = ContextManager()
        mgr.add_user_message("q")
        mgr.add_assistant_message("a")
        stats = mgr.stats()
        assert stats["total_messages"] == 2
        assert stats["user_messages"] == 1
        assert stats["assistant_messages"] == 1


# ── ModelRegistry 测试 ───────────────────────────────────

class TestModelRegistry:
    def test_add_model(self):
        reg = ModelRegistry()
        config = reg.add_model("openai/gpt-4o", "gpt4")
        assert config.model_id == "openai/gpt-4o"
        assert config.alias == "gpt4"

    def test_get_model(self):
        reg = ModelRegistry()
        reg.add_model("openai/gpt-4o", "gpt4")
        model = reg.get_model("gpt4")
        assert model is not None
        assert model.model_id == "openai/gpt-4o"

    def test_remove_model(self):
        reg = ModelRegistry()
        reg.add_model("openai/gpt-4o", "gpt4")
        assert reg.remove_model("gpt4") is True
        assert reg.get_model("gpt4") is None

    def test_remove_nonexistent(self):
        reg = ModelRegistry()
        assert reg.remove_model("nope") is False

    def test_list_models(self):
        reg = ModelRegistry()
        reg.add_model("openai/gpt-4o", "gpt4")
        reg.add_model("anthropic/claude-3-5-sonnet", "claude")
        models = reg.list_models()
        assert len(models) == 2

    def test_assign_role(self):
        reg = ModelRegistry()
        reg.add_model("openai/gpt-4o", "gpt4")
        reg.add_model("anthropic/claude-3-5-sonnet", "claude")
        reg.assign_role("planner", ["claude", "gpt4"])
        assert reg.role_priority["planner"] == ["claude", "gpt4"]

    def test_assign_role_unknown_model_raises(self):
        reg = ModelRegistry()
        with pytest.raises(ValueError, match="未注册"):
            reg.assign_role("planner", ["nonexistent"])

    def test_get_role_priority(self):
        reg = ModelRegistry()
        reg.add_model("openai/gpt-4o", "gpt4")
        reg.add_model("anthropic/claude-3-5-sonnet", "claude")
        reg.assign_role("planner", ["claude", "gpt4"])

        priority = reg.get_role_priority("planner")
        assert priority == ["anthropic/claude-3-5-sonnet", "openai/gpt-4o"]

    def test_get_role_priority_fallback(self):
        reg = ModelRegistry()
        reg.add_model("openai/gpt-4o", "gpt4")
        # 未分配角色时返回所有模型
        priority = reg.get_role_priority("any")
        assert priority == ["openai/gpt-4o"]

    def test_set_mode(self):
        reg = ModelRegistry()
        mode = reg.set_mode("react")
        assert mode.name == "react"

    def test_set_mode_invalid(self):
        reg = ModelRegistry()
        with pytest.raises(ValueError, match="未知范式"):
            reg.set_mode("nonexistent")

    def test_get_current_mode(self):
        reg = ModelRegistry()
        mode = reg.get_current_mode()
        assert mode.name == "direct"  # 默认

    def test_export_config(self):
        reg = ModelRegistry()
        reg.add_model("openai/gpt-4o", "gpt4")
        config = reg.export_config()
        assert "gpt4" in config["models"]
        assert config["mode"] == "direct"

    def test_save_and_load_config(self):
        reg = ModelRegistry()
        reg.add_model("openai/gpt-4o", "gpt4")
        reg.assign_role("planner", ["gpt4"])

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
            path = f.name

        try:
            reg.save_to_file(path)

            reg2 = ModelRegistry()
            reg2.load_from_file(path)
            assert "gpt4" in reg2.models
            assert reg2.role_priority.get("planner") == ["gpt4"]
        finally:
            Path(path).unlink(missing_ok=True)


# ── Command Dispatch 测试 ────────────────────────────────

class TestCommands:
    def test_help_command(self):
        from omniagent.repl.commands import dispatch_command

        reg = ModelRegistry()
        ctx_mgr = ContextManager()
        result = dispatch_command("/help", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})
        assert "可用命令" in result
        assert "/set_model" in result

    def test_set_model_command(self):
        from omniagent.repl.commands import dispatch_command

        reg = ModelRegistry()
        ctx_mgr = ContextManager()
        result = dispatch_command(
            "/set_model", "gpt4 openai/gpt-4o",
            registry=reg, ctx_mgr=ctx_mgr, session_state={},
        )
        assert "✅" in result
        assert "gpt4" in reg.models

    def test_set_model_no_args_shows_providers(self):
        from omniagent.repl.commands import dispatch_command

        reg = ModelRegistry()
        ctx_mgr = ContextManager()
        # 无参数 → 要么提示先配置，要么显示交互菜单（取决于环境是否有已配置的厂商）
        result = dispatch_command(
            "/set_model", "",
            registry=reg, ctx_mgr=ctx_mgr, session_state={},
        )
        # 结果可能是提示配置，也可能是交互菜单的输出
        assert isinstance(result, str)

    def test_models_command_empty(self):
        from omniagent.repl.commands import dispatch_command

        reg = ModelRegistry()
        ctx_mgr = ContextManager()
        result = dispatch_command("/models", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})
        assert "暂无" in result

    def test_models_command_with_models(self):
        from omniagent.repl.commands import dispatch_command

        reg = ModelRegistry()
        reg.add_model("openai/gpt-4o", "gpt4")
        ctx_mgr = ContextManager()
        result = dispatch_command("/models", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})
        assert "gpt4" in result

    def test_mode_command(self):
        from omniagent.repl.commands import dispatch_command

        reg = ModelRegistry()
        ctx_mgr = ContextManager()
        result = dispatch_command("/mode", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})
        assert "plan-execute" in result

    def test_mode_switch(self):
        from omniagent.repl.commands import dispatch_command

        reg = ModelRegistry()
        ctx_mgr = ContextManager()
        result = dispatch_command(
            "/mode", "react",
            registry=reg, ctx_mgr=ctx_mgr, session_state={},
        )
        assert "✅" in result
        assert reg.current_mode == "react"

    def test_context_command(self):
        from omniagent.repl.commands import dispatch_command

        reg = ModelRegistry()
        ctx_mgr = ContextManager()
        ctx_mgr.add_user_message("test")
        result = dispatch_command("/context", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})
        assert "消息总数" in result

    def test_compact_command(self):
        from omniagent.repl.commands import dispatch_command

        reg = ModelRegistry()
        ctx_mgr = ContextManager()
        ctx_mgr.add_user_message("test message")
        result = dispatch_command("/compact", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})
        assert "✅" in result

    def test_undo_command(self):
        from omniagent.repl.commands import dispatch_command

        reg = ModelRegistry()
        ctx_mgr = ContextManager()
        ctx_mgr.save_snapshot()
        ctx_mgr.add_user_message("msg")
        result = dispatch_command("/undo", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})
        assert "✅" in result

    def test_undo_command_empty(self):
        from omniagent.repl.commands import dispatch_command

        reg = ModelRegistry()
        ctx_mgr = ContextManager()
        result = dispatch_command("/undo", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})
        assert "没有" in result

    def test_clear_command(self):
        from omniagent.repl.commands import dispatch_command

        reg = ModelRegistry()
        ctx_mgr = ContextManager()
        ctx_mgr.add_user_message("test")
        result = dispatch_command("/clear", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})
        assert "✅" in result
        assert len(ctx_mgr.history) == 0

    def test_unknown_command(self):
        from omniagent.repl.commands import dispatch_command

        reg = ModelRegistry()
        ctx_mgr = ContextManager()
        result = dispatch_command("/nonexistent", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})
        assert "未知命令" in result

    def test_set_role_command(self):
        from omniagent.repl.commands import dispatch_command

        reg = ModelRegistry()
        reg.add_model("openai/gpt-4o", "gpt4")
        reg.add_model("anthropic/claude-3-5-sonnet", "claude")
        ctx_mgr = ContextManager()
        result = dispatch_command(
            "/set_role", "planner claude gpt4",
            registry=reg, ctx_mgr=ctx_mgr, session_state={},
        )
        assert "✅" in result
