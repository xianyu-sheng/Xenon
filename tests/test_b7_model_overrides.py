"""B7 验收：激活 ModelConfig 死字段 — base_url / api_key 覆盖。"""
from types import SimpleNamespace

import xenon.utils.llm_client as lc


class TestBuildEndpointBaseUrlOverride:
    def test_base_url_override(self):
        ep = lc.build_endpoint(
            "openai/gpt-4o", credentials={"openai": "sk-test"},
            base_url="https://custom.example.com/v1",
        )
        assert ep.base_url == "https://custom.example.com/v1"
        assert ep.api_key == "sk-test"

    def test_default_base_url_when_none(self):
        ep = lc.build_endpoint("openai/gpt-4o", credentials={"openai": "sk-test"})
        assert ep.base_url == "https://api.openai.com/v1"


class TestChatCompletionBaseUrlOverride:
    def test_passes_base_url_to_endpoint(self, monkeypatch):
        captured = {}

        def fake(endpoint, messages, max_tokens, temperature, timeout):
            captured["base_url"] = endpoint.base_url
            return "ok"

        monkeypatch.setattr(lc, "_call_openai_compat", fake)
        lc.chat_completion(
            "openai/gpt-4o", [{"role": "user", "content": "hi"}],
            credentials={"openai": "sk-test"},
            base_url="https://custom.example.com/v1",
        )
        assert captured["base_url"] == "https://custom.example.com/v1"


class TestEngineModelOverrides:
    def test_passes_model_api_key_and_base_url(self, monkeypatch):
        import xenon.engine.base as re_mod
        from xenon.engine.react_engine import ReActEngine

        mc = SimpleNamespace(max_tokens=2048, api_key="sk-per-model",
                             base_url="https://mcp.example.com/v1")
        engine = ReActEngine(["openai/gpt-4o"], model_configs={"openai/gpt-4o": mc})
        captured = {}

        def fake_chat(model_id, messages, **kw):
            captured.update(kw)
            return '{"final_answer":"ok"}'

        monkeypatch.setattr(re_mod, "chat_completion", fake_chat)
        engine._call_llm([{"role": "user", "content": "hi"}])
        assert captured["credentials"] == {"openai": "sk-per-model"}
        assert captured["base_url"] == "https://mcp.example.com/v1"
        assert captured["max_tokens"] == 2048

    def test_no_overrides_when_model_config_empty(self, monkeypatch):
        import xenon.engine.base as re_mod
        from xenon.engine.react_engine import ReActEngine

        engine = ReActEngine(["openai/gpt-4o"])  # 无 model_configs
        captured = {}

        def fake_chat(model_id, messages, **kw):
            captured.update(kw)
            return '{"final_answer":"ok"}'

        monkeypatch.setattr(re_mod, "chat_completion", fake_chat)
        engine._call_llm([{"role": "user", "content": "hi"}])
        assert captured["credentials"] is None
        assert captured["base_url"] is None

    def test_no_api_key_means_no_credentials_override(self, monkeypatch):
        """ModelConfig.api_key 为空时不覆盖全局凭证（交由 build_endpoint 自行加载）。"""
        import xenon.engine.base as re_mod
        from xenon.engine.react_engine import ReActEngine

        mc = SimpleNamespace(max_tokens=2048, api_key="", base_url="https://mcp.example.com/v1")
        engine = ReActEngine(["openai/gpt-4o"], model_configs={"openai/gpt-4o": mc})
        captured = {}

        def fake_chat(model_id, messages, **kw):
            captured.update(kw)
            return '{"final_answer":"ok"}'

        monkeypatch.setattr(re_mod, "chat_completion", fake_chat)
        engine._call_llm([{"role": "user", "content": "hi"}])
        assert captured["credentials"] is None  # 空 key 不覆盖
        assert captured["base_url"] == "https://mcp.example.com/v1"  # base_url 仍生效
