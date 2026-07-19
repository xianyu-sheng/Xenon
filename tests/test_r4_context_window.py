"""R4 验收：ContextManager.max_tokens 从激活模型的 context_window 注入，
替代 128000 硬编码——8k 模型时 needs_compact 不再永不触发，1M 模型不再过早压缩。
"""
from xenon.repl.context_manager import ContextManager
from xenon.repl.model_registry import ModelConfig, ModelRegistry


class TestModelConfigContextWindow:
    def test_default_is_128000(self):
        assert ModelConfig(model_id="openai/gpt-4o", alias="gpt4").context_window == 128000

    def test_add_model_accepts_context_window(self):
        reg = ModelRegistry()
        reg.add_model("openai/gpt-4o", "gpt4", context_window=8000)
        assert reg.models["gpt4"].context_window == 8000


class TestContextWindowFor:
    def test_returns_min_window(self):
        reg = ModelRegistry()
        reg.add_model("openai/gpt-4o", "gpt4", context_window=128000)
        reg.add_model("anthropic/claude", "claude", context_window=200000)
        assert reg.context_window_for(["gpt4", "claude"]) == 128000

    def test_small_window_is_bottleneck(self):
        reg = ModelRegistry()
        reg.add_model("openai/gpt-4o", "gpt4", context_window=8000)
        reg.add_model("anthropic/claude", "claude", context_window=200000)
        assert reg.context_window_for(["gpt4", "claude"]) == 8000

    def test_returns_zero_when_no_aliases(self):
        assert ModelRegistry().context_window_for([]) == 0

    def test_returns_zero_when_alias_unknown(self):
        assert ModelRegistry().context_window_for(["nope"]) == 0


class TestExportLoadRoundTrip:
    def test_context_window_persisted(self, tmp_path):
        reg = ModelRegistry()
        reg.add_model("openai/gpt-4o", "gpt4", context_window=8000)
        p = tmp_path / "models.yaml"
        reg.save_to_file(p)
        reg2 = ModelRegistry()
        reg2.load_from_file(p)
        assert reg2.models["gpt4"].context_window == 8000

    def test_legacy_config_without_context_window_defaults(self, tmp_path):
        """旧配置文件无 context_window 字段时回退默认 128000。"""
        import yaml
        p = tmp_path / "models.yaml"
        p.write_text(yaml.dump({
            "models": {"gpt4": {"model_id": "openai/gpt-4o", "max_tokens": 4096}},
            "roles": {}, "mode": "direct",
        }), encoding="utf-8")
        reg = ModelRegistry()
        reg.load_from_file(p)
        assert reg.models["gpt4"].context_window == 128000


class TestContextManagerInjection:
    def test_injection_makes_compact_trigger_for_small_window(self):
        """R4 核心价值：注入小窗口后 needs_compact 才会正确触发。"""
        reg = ModelRegistry()
        reg.add_model("openai/gpt-4o", "gpt4", context_window=8000)
        cm = ContextManager()  # 默认 128000
        cm.add_user_message("x" * 14000)  # 估算约 7000 token
        # 旧默认 128000：不触发（7000/128000 ≈ 0.055 < 0.8）
        assert not cm.needs_compact()
        # R4 注入 8000 后：触发（7000/8000 = 0.875 ≥ 0.8）
        cm.max_tokens = reg.context_window_for(["gpt4"])
        assert cm.needs_compact()

    def test_large_window_does_not_compact_prematurely(self):
        reg = ModelRegistry()
        reg.add_model("google/gemini", "gemini", context_window=1000000)
        cm = ContextManager()
        cm.max_tokens = reg.context_window_for(["gemini"])
        cm.add_user_message("x" * 14000)
        assert not cm.needs_compact()
