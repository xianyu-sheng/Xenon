"""
C-2 修复测试：env_key 字段真的能读环境变量，anthropic 兼容 ANTHROPIC_AUTH_TOKEN。

v0.3.0 修复前：provider_registry.py 的 env_key 字段只用于 setup_wizard 展示，
_load_credentials() 只从 ~/.omniagent/credentials.yaml 读，env 变量完全被忽略。
结果：Claude Code 内设 ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN 的用户
根本无法用 omniagent（提示"未找到 anthropic 的 API Key"）。

v0.3.0 修复后：
- _resolve_api_key() 三级 fallback：yaml → env_key → anthropic 特殊 ANTHROPIC_AUTH_TOKEN
- get_configured_providers() 用 _resolve_api_key 替换直接读 creds[key]
- _check_first_run 兼容只设 env 的情况
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from omniagent.repl.provider_registry import (
    PROVIDERS,
    _resolve_api_key,
    get_configured_providers,
)


class TestResolveApiKey:
    """C-2 修复：env_key 字段能真的读环境变量，anthropic 兼容 AUTH_TOKEN。"""

    def test_yaml_takes_priority_over_env(self):
        """yaml 里有值 → 不读 env。"""
        info = PROVIDERS["openai"]
        creds = {"openai": "yaml-key"}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "env-key"}):
            assert _resolve_api_key("openai", info, creds) == "yaml-key"

    def test_yaml_empty_falls_back_to_env_key(self):
        """yaml 空 → 读 env_key。"""
        info = PROVIDERS["deepseek"]
        creds = {}  # yaml 完全没有
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env-key"}):
            assert _resolve_api_key("deepseek", info, creds) == "env-key"

    def test_yaml_empty_env_empty_returns_empty(self):
        """yaml 空、env 空 → 返回空串。"""
        info = PROVIDERS["openai"]
        creds = {}
        with patch.dict(os.environ, {}, clear=True):
            # 清掉所有相关 env
            os.environ.pop("OPENAI_API_KEY", None)
            assert _resolve_api_key("openai", info, creds) == ""

    def test_anthropic_falls_back_to_auth_token(self):
        """anthropic 厂商：ANTHROPIC_API_KEY 空时 fallback ANTHROPIC_AUTH_TOKEN。"""
        info = PROVIDERS["anthropic"]
        creds = {}  # yaml 没设
        env = {"ANTHROPIC_AUTH_TOKEN": "claude-code-token"}
        with patch.dict(os.environ, env, clear=True):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            assert _resolve_api_key("anthropic", info, creds) == "claude-code-token"

    def test_anthropic_api_key_still_priority(self):
        """ANTHROPIC_API_KEY 仍优先于 ANTHROPIC_AUTH_TOKEN。"""
        info = PROVIDERS["anthropic"]
        creds = {}
        env = {
            "ANTHROPIC_API_KEY": "primary-key",
            "ANTHROPIC_AUTH_TOKEN": "fallback-key",
        }
        with patch.dict(os.environ, env, clear=True):
            assert _resolve_api_key("anthropic", info, creds) == "primary-key"

    def test_non_anthropic_ignores_auth_token(self):
        """非 anthropic 厂商不认 ANTHROPIC_AUTH_TOKEN。"""
        info = PROVIDERS["openai"]
        creds = {}
        with patch.dict(
            os.environ,
            {"ANTHROPIC_AUTH_TOKEN": "irrelevant"},
            clear=True,
        ):
            os.environ.pop("OPENAI_API_KEY", None)
            # ANTHROPIC_AUTH_TOKEN 不是 openai 的 env_key
            assert _resolve_api_key("openai", info, creds) == ""


class TestGetConfiguredProvidersEnvFallback:
    """C-2 修复：get_configured_providers 走 env fallback。"""

    def test_env_only_anthropic_appears_in_configured(self):
        """只设 ANTHROPIC_AUTH_TOKEN（没 yaml）→ anthropic 出现在 configured。"""
        env = {"ANTHROPIC_AUTH_TOKEN": "claude-code-token", "HOME": os.environ.get("HOME", "/tmp")}
        with patch.dict(os.environ, env, clear=True):
            # 清 yaml：mock load_credentials 返回空
            with patch(
                "omniagent.repl.provider_registry.load_credentials",
                return_value={},
            ):
                # 避免实际 HTTP 调用 fetch_provider_models
                with patch(
                    "omniagent.repl.provider_registry.fetch_provider_models",
                    return_value=["claude-sonnet-4-20250514"],
                ):
                    configured = get_configured_providers()
                    keys = [p.key for p in configured]
                    assert "anthropic" in keys, f"anthropic 应在 configured 中，实际: {keys}"
                    p = next(p for p in configured if p.key == "anthropic")
                    assert p.api_key == "claude-code-token"

    def test_yaml_and_env_both_set_yaml_wins(self):
        """yaml 和 env 都设了 → yaml 胜出。"""
        env = {
            "DEEPSEEK_API_KEY": "env-key",
            "HOME": os.environ.get("HOME", "/tmp"),
        }
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "omniagent.repl.provider_registry.load_credentials",
                return_value={"deepseek": "yaml-key"},
            ):
                with patch(
                    "omniagent.repl.provider_registry.fetch_provider_models",
                    return_value=["deepseek-v4-pro"],
                ):
                    configured = get_configured_providers()
                    p = next(p for p in configured if p.key == "deepseek")
                    assert p.api_key == "yaml-key"

    def test_env_only_ollama_appears(self):
        """只设 OLLAMA_API_KEY → ollama 出现在 configured。"""
        env = {
            "OLLAMA_API_KEY": "ollama-key",
            "HOME": os.environ.get("HOME", "/tmp"),
        }
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "omniagent.repl.provider_registry.load_credentials",
                return_value={},
            ):
                with patch(
                    "omniagent.repl.provider_registry.fetch_provider_models",
                    return_value=["llama3"],
                ):
                    configured = get_configured_providers()
                    keys = [p.key for p in configured]
                    assert "ollama" in keys
                    p = next(p for p in configured if p.key == "ollama")
                    assert p.api_key == "ollama-key"
