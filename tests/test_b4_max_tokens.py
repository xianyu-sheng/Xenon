"""B4 验收：去除 max_tokens=131072 硬编码 + chat_completion 按厂商上限钳制。"""
from types import SimpleNamespace

import omniagent.utils.llm_client as lc


class TestChatCompletionClampsMaxTokens:
    def test_openai_clamped_to_cap(self, monkeypatch):
        captured = {}

        def fake(endpoint, messages, max_tokens, temperature, timeout):
            captured["mt"] = max_tokens
            return "ok"

        monkeypatch.setattr(lc, "_call_openai_compat", fake)
        lc.chat_completion(
            "openai/gpt-4o", [{"role": "user", "content": "hi"}],
            credentials={"openai": "sk-test"}, max_tokens=131072,
        )
        assert captured["mt"] == 16384  # openai 厂商上限

    def test_anthropic_clamped_to_cap(self, monkeypatch):
        captured = {}

        def fake(endpoint, messages, max_tokens, temperature, timeout):
            captured["mt"] = max_tokens
            return "ok"

        monkeypatch.setattr(lc, "_call_anthropic", fake)
        lc.chat_completion(
            "anthropic/claude-3-5-sonnet", [{"role": "user", "content": "hi"}],
            credentials={"anthropic": "sk-test"}, max_tokens=131072,
        )
        assert captured["mt"] == 8192  # anthropic 厂商上限

    def test_below_cap_unchanged(self, monkeypatch):
        captured = {}

        def fake(endpoint, messages, max_tokens, temperature, timeout):
            captured["mt"] = max_tokens
            return "ok"

        monkeypatch.setattr(lc, "_call_openai_compat", fake)
        lc.chat_completion(
            "openai/gpt-4o", [{"role": "user", "content": "hi"}],
            credentials={"openai": "sk-test"}, max_tokens=1000,
        )
        assert captured["mt"] == 1000


class TestEngineReadsModelConfigMaxTokens:
    def test_react_reads_model_config(self, monkeypatch):
        import omniagent.engine.base as re_mod
        from omniagent.engine.react_engine import ReActEngine

        mc = SimpleNamespace(max_tokens=2048)
        engine = ReActEngine(["openai/gpt-4o"], model_configs={"openai/gpt-4o": mc})
        captured = {}

        def fake_chat(model_id, messages, *, max_tokens=None, **kw):
            captured["mt"] = max_tokens
            return '{"final_answer":"ok"}'

        monkeypatch.setattr(re_mod, "chat_completion", fake_chat)
        engine._call_llm([{"role": "user", "content": "hi"}])
        assert captured["mt"] == 2048  # 来自 ModelConfig，而非 131072

    def test_react_falls_back_to_8192_without_config(self, monkeypatch):
        import omniagent.engine.base as re_mod
        from omniagent.engine.react_engine import ReActEngine

        engine = ReActEngine(["openai/gpt-4o"])  # 无 model_configs
        captured = {}

        def fake_chat(model_id, messages, *, max_tokens=None, **kw):
            captured["mt"] = max_tokens
            return '{"final_answer":"ok"}'

        monkeypatch.setattr(re_mod, "chat_completion", fake_chat)
        engine._call_llm([{"role": "user", "content": "hi"}])
        assert captured["mt"] == 8192  # 安全默认，而非 131072

    def test_explicit_max_tokens_takes_priority(self, monkeypatch):
        import omniagent.engine.base as re_mod
        from omniagent.engine.react_engine import ReActEngine

        mc = SimpleNamespace(max_tokens=2048)
        engine = ReActEngine(["openai/gpt-4o"], model_configs={"openai/gpt-4o": mc})
        captured = {}

        def fake_chat(model_id, messages, *, max_tokens=None, **kw):
            captured["mt"] = max_tokens
            return '{"final_answer":"ok"}'

        monkeypatch.setattr(re_mod, "chat_completion", fake_chat)
        engine._call_llm([{"role": "user", "content": "hi"}], max_tokens=4096)
        assert captured["mt"] == 4096  # 显式入参优先
