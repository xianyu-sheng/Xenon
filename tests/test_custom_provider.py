"""v0.4.0 Step 1: 动态模型商注册测试."""
from __future__ import annotations

from unittest.mock import patch
from xenon.repl.provider_registry import (
    register_custom_provider,
    remove_custom_provider,
    _load_custom_providers,
    PROVIDERS,
)


class TestCustomProviderRegistration:
    """动态模型商注册测试."""

    def test_register_custom_provider_generates_key(self, tmp_path, monkeypatch):
        """注册自定义模型商自动生成 key."""
        monkeypatch.setattr(
            "xenon.repl.provider_registry.CREDENTIALS_PATH",
            tmp_path / "creds.yaml",
        )
        with patch("xenon.repl.provider_registry.fetch_provider_models",
                   return_value=["model-a", "model-b"]):
            info = register_custom_provider(
                "字节豆包", "https://ark.example.com/api/v3", "sk-test"
            )
        assert info.name == "字节豆包"
        assert "model-a" in info.models
        assert info.base_url == "https://ark.example.com/api/v3"

    def test_custom_provider_persisted_to_yaml(self, tmp_path, monkeypatch):
        """自定义模型商持久化到 credentials.yaml."""
        monkeypatch.setattr(
            "xenon.repl.provider_registry.CREDENTIALS_PATH",
            tmp_path / "creds.yaml",
        )
        with patch("xenon.repl.provider_registry.fetch_provider_models",
                   return_value=["m1"]):
            register_custom_provider("Test", "https://test.api/v1", "sk-abc")

        custom = _load_custom_providers()
        assert len(custom) == 1
        key = list(custom.keys())[0]
        assert custom[key]["name"] == "Test"
        assert custom[key]["api_key"] == "sk-abc"

    def test_remove_custom_provider(self, tmp_path, monkeypatch):
        """删除自定义模型商."""
        monkeypatch.setattr(
            "xenon.repl.provider_registry.CREDENTIALS_PATH",
            tmp_path / "creds.yaml",
        )
        with patch("xenon.repl.provider_registry.fetch_provider_models",
                   return_value=["m1"]):
            info = register_custom_provider("X", "https://x/v1", "sk")

        assert remove_custom_provider(info.key) is True
        assert remove_custom_provider(info.key) is False  # already gone

    def test_empty_custom_providers_returns_empty_dict(self, tmp_path, monkeypatch):
        """无自定义模型商时返回空 dict."""
        monkeypatch.setattr(
            "xenon.repl.provider_registry.CREDENTIALS_PATH",
            tmp_path / "nonexistent.yaml",
        )
        assert _load_custom_providers() == {}

    def test_builtin_providers_unchanged(self):
        """内置模型商不受影响."""
        assert "openai" in PROVIDERS
        assert "deepseek" in PROVIDERS
        assert PROVIDERS["openai"].name == "OpenAI"
