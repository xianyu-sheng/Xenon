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

    def test_stats_reuses_token_usage_cache(self, monkeypatch):
        mgr = ContextManager()
        calls = []

        def fake_estimate(text):
            calls.append(text)
            return len(text)

        monkeypatch.setattr(mgr, "estimate_tokens", fake_estimate)

        mgr.add_user_message("hello")
        assert mgr.stats()["estimated_tokens"] == 5
        assert mgr.stats()["estimated_tokens"] == 5
        assert calls == ["hello"]

        mgr.add_assistant_message("world")
        assert mgr.stats()["estimated_tokens"] == 10
        assert calls == ["hello", "world"]


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


# ── ProviderRegistry 测试 ────────────────────────────────

class TestProviderRegistry:
    def test_get_configured_providers_refreshes_openai_models(self, monkeypatch):
        import omniagent.repl.provider_registry as provider_registry
        provider_registry.clear_model_list_cache()

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": [
                        {"id": "gpt-4o", "created": 1715367049},
                        {"id": "gpt-5", "created": 1750000000},
                        {"id": "gpt-5.5", "created": 1780000000},
                    ]
                }

        calls = []

        def fake_get(url, headers, timeout):
            calls.append((url, headers, timeout))
            return FakeResponse()

        monkeypatch.setattr(provider_registry, "load_credentials", lambda: {"openai": "sk-test"})
        monkeypatch.setattr(provider_registry.httpx, "get", fake_get)

        configured = provider_registry.get_configured_providers()
        openai = next(p for p in configured if p.key == "openai")

        assert openai.models == ["gpt-5.5", "gpt-5", "gpt-4o"]
        assert calls[0][0] == "https://api.openai.com/v1/models"
        assert calls[0][1]["Accept"] == "application/json"
        assert calls[0][1]["Authorization"] == "Bearer sk-test"

    def test_get_configured_providers_uses_custom_openai_base_url(self, monkeypatch):
        import omniagent.repl.provider_registry as provider_registry
        provider_registry.clear_model_list_cache()

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"id": "gpt-5.5"}, {"id": "gpt-5"}]}

        calls = []

        def fake_get(url, headers, timeout):
            calls.append((url, headers, timeout))
            return FakeResponse()

        monkeypatch.setattr(
            provider_registry,
            "load_provider_configs",
            lambda: {
                "openai": provider_registry.ProviderCredential(
                    api_key="sk-relay",
                    base_url="https://codex.gogogpt.net",
                )
            },
        )
        monkeypatch.setattr(provider_registry.httpx, "get", fake_get)

        configured = provider_registry.get_configured_providers()
        openai = next(p for p in configured if p.key == "openai")

        assert openai.base_url == "https://codex.gogogpt.net"
        assert openai.models == ["gpt-5.5", "gpt-5"]
        assert calls[0][0] == "https://codex.gogogpt.net/models"

    def test_openai_relay_model_list_tries_v1_fallback(self, monkeypatch):
        import omniagent.repl.provider_registry as provider_registry
        provider_registry.clear_model_list_cache()

        class FakeResponse:
            def __init__(self, ok):
                self.ok = ok

            def raise_for_status(self):
                if not self.ok:
                    raise provider_registry.httpx.HTTPStatusError(
                        "not found",
                        request=provider_registry.httpx.Request("GET", "https://relay.test/models"),
                        response=provider_registry.httpx.Response(404),
                    )

            def json(self):
                return {"data": [{"id": "gpt-5.5"}]}

        calls = []

        def fake_get(url, headers, timeout):
            calls.append(url)
            return FakeResponse(url.endswith("/v1/models"))

        provider = provider_registry.ProviderInfo(
            name="OpenAI Relay",
            key="openai",
            base_url="https://relay.test",
            env_key="OPENAI_API_KEY",
            models=[],
            api_key="sk-test",
        )
        monkeypatch.setattr(provider_registry.httpx, "get", fake_get)

        models = provider_registry.fetch_provider_models(provider, "sk-test")

        assert models == ["gpt-5.5"]
        assert calls == ["https://relay.test/models", "https://relay.test/v1/models"]

    def test_get_configured_providers_refreshes_deepseek_models(self, monkeypatch):
        import omniagent.repl.provider_registry as provider_registry
        provider_registry.clear_model_list_cache()

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"id": "deepseek-v4-pro"}, {"id": "deepseek-v4-flash"}]}

        calls = []

        def fake_get(url, headers, timeout):
            calls.append((url, headers, timeout))
            return FakeResponse()

        monkeypatch.setattr(provider_registry, "load_credentials", lambda: {"deepseek": "sk-test"})
        monkeypatch.setattr(provider_registry.httpx, "get", fake_get)

        configured = provider_registry.get_configured_providers()
        deepseek = next(p for p in configured if p.key == "deepseek")

        assert deepseek.models == ["deepseek-v4-pro", "deepseek-v4-flash"]
        assert calls[0][0] == "https://api.deepseek.com/models"
        assert calls[0][1]["Accept"] == "application/json"
        assert calls[0][1]["Authorization"] == "Bearer sk-test"

    def test_refresh_failure_does_not_show_stale_builtin_models(self, monkeypatch):
        import omniagent.repl.provider_registry as provider_registry
        provider_registry.clear_model_list_cache()

        def fake_get(url, headers, timeout):
            raise provider_registry.httpx.ConnectError("network down")

        monkeypatch.setattr(provider_registry, "load_credentials", lambda: {"openai": "sk-test"})
        monkeypatch.setattr(provider_registry.httpx, "get", fake_get)

        configured = provider_registry.get_configured_providers()
        openai = next(p for p in configured if p.key == "openai")

        assert openai.models == []

    def test_fetch_provider_models_uses_anthropic_headers_and_pagination(self, monkeypatch):
        import omniagent.repl.provider_registry as provider_registry
        provider_registry.clear_model_list_cache()

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self.payload

        payloads = [
            {
                "data": [
                    {"id": "claude-4.5-sonnet-20260601", "created_at": "2026-06-01T00:00:00Z"},
                    {"id": "claude-sonnet-4-20250514", "created_at": "2025-05-14T00:00:00Z"},
                ],
                "has_more": True,
                "last_id": "claude-sonnet-4-20250514",
            },
            {
                "data": [
                    {"id": "claude-3-5-sonnet-20241022", "created_at": "2024-10-22T00:00:00Z"},
                ],
                "has_more": False,
            },
        ]
        calls = []

        def fake_get(url, headers, timeout):
            calls.append((url, headers, timeout))
            return FakeResponse(payloads[len(calls) - 1])

        monkeypatch.setattr(provider_registry.httpx, "get", fake_get)

        models = provider_registry.fetch_provider_models(
            provider_registry.PROVIDERS["anthropic"],
            "sk-ant-test",
        )

        assert models == [
            "claude-4.5-sonnet-20260601",
            "claude-sonnet-4-20250514",
            "claude-3-5-sonnet-20241022",
        ]
        assert calls[0][0] == "https://api.anthropic.com/v1/models"
        assert calls[1][0] == "https://api.anthropic.com/v1/models?after_id=claude-sonnet-4-20250514"
        assert calls[0][1]["x-api-key"] == "sk-ant-test"
        assert calls[0][1]["anthropic-version"] == "2023-06-01"

    def test_fetch_provider_models_uses_short_lived_success_cache(self, monkeypatch):
        import omniagent.repl.provider_registry as provider_registry

        provider_registry.clear_model_list_cache()

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"id": "gpt-5.5"}]}

        calls = []

        def fake_get(url, headers, timeout):
            calls.append(url)
            return FakeResponse()

        monkeypatch.setattr(provider_registry.httpx, "get", fake_get)

        provider = provider_registry.PROVIDERS["openai"]
        first = provider_registry.fetch_provider_models(provider, "sk-cache")
        second = provider_registry.fetch_provider_models(provider, "sk-cache")

        assert first == ["gpt-5.5"]
        assert second == ["gpt-5.5"]
        assert calls == ["https://api.openai.com/v1/models"]

    def test_fetch_provider_models_uses_failure_cooldown(self, monkeypatch):
        import omniagent.repl.provider_registry as provider_registry

        provider_registry.clear_model_list_cache()
        calls = []

        def fake_get(url, headers, timeout):
            calls.append(url)
            raise provider_registry.httpx.ConnectError("network down")

        monkeypatch.setattr(provider_registry.httpx, "get", fake_get)

        provider = provider_registry.PROVIDERS["openai"]
        first = provider_registry.fetch_provider_models(provider, "sk-fail")
        second = provider_registry.fetch_provider_models(provider, "sk-fail")

        assert first == []
        assert second == []
        assert calls == ["https://api.openai.com/v1/models"]


# ── Command Dispatch 测试 ────────────────────────────────

class TestCommands:
    def test_help_command(self):
        from omniagent.repl.commands import dispatch_command

        reg = ModelRegistry()
        ctx_mgr = ContextManager()
        result = dispatch_command("/help", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})
        assert "可用命令" in result
        assert "/set_model" in result
        assert "/set_up" in result

    def test_set_up_alias_is_registered(self):
        from omniagent.repl.commands import dispatch_command

        reg = ModelRegistry()
        ctx_mgr = ContextManager()

        result = dispatch_command(
            "/set_up",
            "",
            registry=reg,
            ctx_mgr=ctx_mgr,
            session_state={},
        )

        assert "未知命令" not in result
        assert "无法获取 REPL 状态" in result

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

    def test_set_model_no_live_models_returns_clear_error(self, monkeypatch):
        from omniagent.repl.commands import dispatch_command
        import omniagent.repl.provider_registry as provider_registry
        from omniagent.repl.provider_registry import ProviderInfo

        reg = ModelRegistry()
        ctx_mgr = ContextManager()
        provider = ProviderInfo(
            name="OpenAI",
            key="openai",
            base_url="https://api.openai.com/v1",
            env_key="OPENAI_API_KEY",
            models=[],
            api_key="sk-test",
        )
        monkeypatch.setattr(provider_registry, "get_configured_providers", lambda: [provider])

        result = dispatch_command("/set_model", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})

        assert "未能实时获取任何模型" in result

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

    def test_runs_command_lists_and_shows_run(self, monkeypatch, tmp_path):
        from omniagent.engine.run_recorder import RunRecorder
        from omniagent.repl.commands import dispatch_command
        from omniagent.repl.session import RuntimeSessionStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeSessionStore(root=tmp_path / ".omniagent" / "sessions")
        session = store.create(title="Test Session", session_id="sess-runs")
        recorder = RunRecorder(
            goal="测试 run 命令",
            mode="react",
            model_ids=["test/model"],
            root=session.runs_dir,
            run_id="run-command-test",
            session_id=session.id,
        )
        recorder.start()
        recorder.emit("tool.call_started", tool_name="read_file", params={"file_path": "README.md"})
        recorder.finish(status="success", result="done")

        reg = ModelRegistry()
        ctx_mgr = ContextManager()
        state = {"_session_store": store, "_runtime_session": session}

        listing = dispatch_command("/runs", "", registry=reg, ctx_mgr=ctx_mgr, session_state=state)
        detail = dispatch_command("/runs", "run-command-test", registry=reg, ctx_mgr=ctx_mgr, session_state=state)

        assert listing is not None
        assert "run-command-test" in listing
        assert "测试 run 命令" in listing
        assert detail is not None
        assert "sess-runs" in detail
        assert "tool.call_started" in detail
        assert "events.jsonl" in detail

    def test_session_and_notes_commands(self, tmp_path):
        from omniagent.repl.commands import dispatch_command
        from omniagent.repl.session import RuntimeSessionStore

        store = RuntimeSessionStore(root=tmp_path)
        session = store.create(title="Test Session", session_id="sess-command")
        store.append_message(session.id, role="user", content="hello", run_id="run-1")
        state = {"_session_store": store, "_runtime_session": session}

        reg = ModelRegistry()
        ctx_mgr = ContextManager()

        summary = dispatch_command("/session", "", registry=reg, ctx_mgr=ctx_mgr, session_state=state)
        thread = dispatch_command("/session", "thread", registry=reg, ctx_mgr=ctx_mgr, session_state=state)
        added = dispatch_command("/notes", "add remember this", registry=reg, ctx_mgr=ctx_mgr, session_state=state)
        notes = dispatch_command("/notes", "", registry=reg, ctx_mgr=ctx_mgr, session_state=state)

        assert summary is not None and "sess-command" in summary
        assert thread is not None and "hello" in thread
        assert added is not None and "已追加" in added
        assert notes is not None and "remember this" in notes


# ── Tool Detection 测试 ──────────────────────────────────

class TestToolDetection:
    """测试 direct 模式自动工具委派检测。"""

    def test_repl_append_thread_message_records_current_session(self, monkeypatch, tmp_path):
        from omniagent.repl import repl as repl_module
        from omniagent.repl.repl import REPL
        from omniagent.repl.session import RuntimeSessionStore

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(repl_module, "RuntimeSessionStore", lambda: RuntimeSessionStore(root=tmp_path / "sessions"))

        repl = REPL()
        repl._append_thread_message("user", "hello", run_id="run-1", metadata={"mode": "direct"})

        entries = repl.session_store.read_thread(repl.runtime_session.id)

        assert len(entries) == 1
        assert entries[0]["role"] == "user"
        assert entries[0]["run_id"] == "run-1"
        assert entries[0]["metadata"]["mode"] == "direct"

    def test_repl_start_run_uses_session_runs_dir(self, monkeypatch, tmp_path):
        from omniagent.repl import repl as repl_module
        from omniagent.repl.repl import REPL
        from omniagent.repl.session import RuntimeSessionStore

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(repl_module, "RuntimeSessionStore", lambda: RuntimeSessionStore(root=tmp_path / "sessions"))

        repl = REPL()
        recorder = repl._start_run("hello", "direct", ["test/model"])

        assert recorder.session_id == repl.runtime_session.id
        assert recorder.events_path.exists()
        assert recorder.events_path.parent.parent == repl.runtime_session.runs_dir

    def test_file_write_chinese(self):
        from omniagent.repl.repl import REPL
        assert REPL._detect_tool_need("帮我创建一个 hello.py 文件") is True
        assert REPL._detect_tool_need("写入文件到 config.yaml") is True
        assert REPL._detect_tool_need("保存这个文件") is True
        assert REPL._detect_tool_need("生成一个 README.md 文件") is True

    def test_file_write_english(self):
        from omniagent.repl.repl import REPL
        assert REPL._detect_tool_need("write a file to hello.py") is True
        assert REPL._detect_tool_need("create a new config file") is True
        assert REPL._detect_tool_need("save this to disk") is True

    def test_file_read_chinese(self):
        from omniagent.repl.repl import REPL
        assert REPL._detect_tool_need("读取 config.yaml 文件") is True
        assert REPL._detect_tool_need("查看 main.py 的内容") is True
        assert REPL._detect_tool_need("打开这个文件看看") is True

    def test_file_read_english(self):
        from omniagent.repl.repl import REPL
        assert REPL._detect_tool_need("read the config file") is True
        assert REPL._detect_tool_need("show me the content of main.py") is True

    def test_file_edit_chinese(self):
        from omniagent.repl.repl import REPL
        assert REPL._detect_tool_need("修改 main.py 文件中的函数") is True
        assert REPL._detect_tool_need("编辑 config.yaml 的配置") is True
        assert REPL._detect_tool_need("替换文件中的 TODO") is True

    def test_file_edit_english(self):
        from omniagent.repl.repl import REPL
        assert REPL._detect_tool_need("edit the main.py file") is True
        assert REPL._detect_tool_need("modify the config.yaml") is True

    def test_command_execution(self):
        from omniagent.repl.repl import REPL
        assert REPL._detect_tool_need("执行命令 python main.py") is True
        assert REPL._detect_tool_need("运行 pytest 测试") is True
        assert REPL._detect_tool_need("run the test suite") is True

    def test_git_operations(self):
        from omniagent.repl.repl import REPL
        assert REPL._detect_tool_need("git commit -m 'fix'") is True
        assert REPL._detect_tool_need("git push origin main") is True
        assert REPL._detect_tool_need("提交代码到 git") is True
        assert REPL._detect_tool_need("推送代码到远程仓库") is True

    def test_search(self):
        from omniagent.repl.repl import REPL
        assert REPL._detect_tool_need("搜索文件中的 TODO") is True
        assert REPL._detect_tool_need("find all Python files") is True
        assert REPL._detect_tool_need("grep for error messages") is True

    def test_web_fetch(self):
        from omniagent.repl.repl import REPL
        assert REPL._detect_tool_need("抓取这个网页的内容") is True
        assert REPL._detect_tool_need("fetch the page at https://example.com") is True

    def test_file_path_pattern(self):
        from omniagent.repl.repl import REPL
        assert REPL._detect_tool_need("看看 src/main.py 怎么写的") is True
        assert REPL._detect_tool_need("打开 ./config.yaml") is True
        assert REPL._detect_tool_need("检查 tests/test_tools.py") is True
        assert REPL._detect_tool_need("看 C:\\Users\\test\\main.py") is True

    def test_list_files(self):
        from omniagent.repl.repl import REPL
        assert REPL._detect_tool_need("列出当前目录的文件") is True
        assert REPL._detect_tool_need("查看文件夹内容") is True
        assert REPL._detect_tool_need("list all files") is True

    def test_no_tool_needed(self):
        from omniagent.repl.repl import REPL
        assert REPL._detect_tool_need("什么是快速排序？") is False
        assert REPL._detect_tool_need("解释一下 Python 的装饰器") is False
        assert REPL._detect_tool_need("帮我分析这段代码的逻辑") is False
        assert REPL._detect_tool_need("今天天气怎么样") is False
        assert REPL._detect_tool_need("how does machine learning work") is False
        assert REPL._detect_tool_need("你好") is False
