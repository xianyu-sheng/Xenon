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


# ── Mock 辅助函数 ──────────────────────────────────────────

def _make_mock_client(fake_get):
    """
    创建一个假的 _create_http_client 工厂，用于在测试中替换
    provider_registry._create_http_client。

    fake_get(url, headers) -> FakeResponse 是一个可调用对象，
    返回模拟的 HTTP 响应。
    """
    from contextlib import contextmanager

    class MockClient:
        def __init__(self, get_fn):
            self._get_fn = get_fn
            self._calls: list[tuple] = []

        def get(self, url, *, headers):
            self._calls.append((url, headers))
            return self._get_fn(url, headers)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    @contextmanager
    def _factory(*args, **kwargs):
        client = MockClient(fake_get)
        yield client

    return _factory


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
        # F3 三层策略：需 usage_ratio 落在 60-85%（Tier 2）才会触发压缩。
        # 无 model_priority 时 Tier 2 的 6 段 LLM 路径无可用模型 → 回退 _auto_summary。
        mgr = ContextManager(max_tokens=250)
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


# ── ProviderRegistry 测试 ────────────────────────────────

class TestProviderRegistry:
    def test_get_configured_providers_refreshes_openai_models(self, monkeypatch):
        import omniagent.repl.provider_registry as provider_registry

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

        def fake_get(url, headers):
            calls.append((url, headers))
            return FakeResponse()

        monkeypatch.setattr(provider_registry, "load_credentials", lambda: {"openai": "sk-test"})
        monkeypatch.setattr(provider_registry, "_create_http_client", _make_mock_client(fake_get))

        configured = provider_registry.get_configured_providers()
        openai = next(p for p in configured if p.key == "openai")

        assert openai.models == ["gpt-5.5", "gpt-5", "gpt-4o"]
        assert calls[0][0] == "https://api.openai.com/v1/models"
        assert calls[0][1]["Accept"] == "application/json"
        assert calls[0][1]["Authorization"] == "Bearer sk-test"

    def test_get_configured_providers_refreshes_deepseek_models(self, monkeypatch):
        import omniagent.repl.provider_registry as provider_registry

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"id": "deepseek-v4-pro"}, {"id": "deepseek-v4-flash"}]}

        calls = []

        def fake_get(url, headers):
            calls.append((url, headers))
            return FakeResponse()

        monkeypatch.setattr(provider_registry, "load_credentials", lambda: {"deepseek": "sk-test"})
        monkeypatch.setattr(provider_registry, "_create_http_client", _make_mock_client(fake_get))

        configured = provider_registry.get_configured_providers()
        deepseek = next(p for p in configured if p.key == "deepseek")

        assert deepseek.models == ["deepseek-v4-pro", "deepseek-v4-flash"]
        assert calls[0][0] == "https://api.deepseek.com/models"
        assert calls[0][1]["Accept"] == "application/json"
        assert calls[0][1]["Authorization"] == "Bearer sk-test"

    def test_refresh_failure_does_not_show_stale_builtin_models(self, monkeypatch):
        import omniagent.repl.provider_registry as provider_registry

        def fake_get(url, headers):
            raise provider_registry.httpx.ConnectError("network down")

        monkeypatch.setattr(provider_registry, "load_credentials", lambda: {"openai": "sk-test"})
        monkeypatch.setattr(provider_registry, "_create_http_client", _make_mock_client(fake_get))

        configured = provider_registry.get_configured_providers()
        openai = next(p for p in configured if p.key == "openai")

        assert openai.models == []

    def test_fetch_provider_models_uses_anthropic_headers_and_pagination(self, monkeypatch):
        import omniagent.repl.provider_registry as provider_registry

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

        def fake_get(url, headers):
            calls.append((url, headers))
            return FakeResponse(payloads[len(calls) - 1])

        monkeypatch.setattr(provider_registry, "_create_http_client", _make_mock_client(fake_get))

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


# ── Tool Detection 测试 ──────────────────────────────────

class TestToolDetection:
    """测试 direct 模式自动工具委派检测。"""

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

    def test_query_intent_routes_to_react(self):
        """query 意图（天气/价格/汇率/新闻等实时数据）必然需要工具 → 路由 ReAct。

        回归：direct 模式不向 API 传工具，而 prompt_optimizer 会注入"使用工具获取
        实时数据"指令，LLM 无工具可调时只给前言式回复（"我来帮你查询…"）。故 query
        意图直接判 True，绕过 direct 走 ReAct。
        """
        from omniagent.repl.repl import REPL
        from omniagent.repl.prompt_optimizer import detect_intent

        # 这些输入应被识别为 query 意图，且需要工具
        cases = [
            "今天苏州的天气怎么样",
            "今天黄金价格多少",
            "现在美元兑人民币汇率多少",
            "查看今天的科技新闻",
        ]
        for text in cases:
            assert detect_intent(text) == "query", f"意图识别失败: {text}"
            assert REPL._detect_tool_need(text, intent="query") is True, f"query 应触发工具: {text}"

    def test_non_query_intent_not_auto_triggered(self):
        """非 query 意图（chat/explain）且无编程/文件/命令关键词时不触发工具。"""
        from omniagent.repl.repl import REPL
        # chat 意图 + 无工具关键词 → False
        assert REPL._detect_tool_need("你好", intent="chat") is False
        assert REPL._detect_tool_need("谢谢", intent="chat") is False
        # explain 意图 + 无工具关键词 → False
        assert REPL._detect_tool_need("解释一下装饰器", intent="explain") is False
        # 无 intent 时，天气等实时查询不触发（无天气正则，避免误判）
        assert REPL._detect_tool_need("今天天气怎么样", intent=None) is False

    @pytest.mark.parametrize("text", [
        "写一个 Python 爬虫",                          # 缺"帮我/请/给"前缀
        "用 JS 写一个待办事项应用",                    # 缺前缀 + 应用不在触发词
        "我想写一个 Python 脚本查询天气",              # "我想"不在"帮我/请/给"内
        "写一段 Go 代码实现 HTTP 服务",                # 缺前缀
        "用 Rust 写一个命令行工具",                    # 缺前缀
    ])
    def test_write_code_intent_routes_to_react(self, text):
        """P2-修复1（B-1）：write_code 意图同样兜底路由到 ReAct，与 query 同根。

        _TOOL_PATTERNS 中编程类正则要求"帮我/请/给"前缀，无法覆盖"写一个 X"/
        "用 Y 写一个 Z" 等自然语序，导致 detect_intent 已识别为 write_code，
        但 _detect_tool_need 返回 False 走 direct LLM，LLM 凭空"写"代码。
        """
        from omniagent.repl.repl import REPL
        from omniagent.repl.prompt_optimizer import detect_intent

        # 确认意图识别是 write_code
        assert detect_intent(text) == "write_code", f"意图识别失败: {text}"
        # write_code 意图必须路由 ReAct
        assert REPL._detect_tool_need(text, intent="write_code") is True, (
            f"write_code 意图应触发工具路由: {text}"
        )

