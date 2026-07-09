"""
C-2 延伸：llm_client.py 独立 _load_credentials() 也得认 ANTHROPIC_AUTH_TOKEN，
build_endpoint() 也要认 ANTHROPIC_BASE_URL env（Claude Code / SDK 兼容）。
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from omniagent.utils.llm_client import _load_credentials, build_endpoint


class TestLLMClientAuthTokenFallback:
    """C-2 延伸：llm_client 兼容 ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL。"""

    def test_anthropic_auth_token_used_when_api_key_missing(self):
        """ANTHROPIC_API_KEY 空时 → 用 ANTHROPIC_AUTH_TOKEN。"""
        env = {
            "ANTHROPIC_AUTH_TOKEN": "claude-code-token",
        }
        with patch.dict(os.environ, env, clear=True):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            creds = _load_credentials()
            assert creds.get("anthropic") == "claude-code-token"

    def test_anthropic_api_key_still_priority(self):
        """ANTHROPIC_API_KEY 仍优先于 ANTHROPIC_AUTH_TOKEN。"""
        env = {
            "ANTHROPIC_API_KEY": "primary",
            "ANTHROPIC_AUTH_TOKEN": "fallback",
        }
        with patch.dict(os.environ, env, clear=True):
            creds = _load_credentials()
            assert creds.get("anthropic") == "primary"

    def test_auth_token_does_not_pollute_other_providers(self):
        """ANTHROPIC_AUTH_TOKEN 只影响 anthropic，不污染其他厂商。"""
        env = {
            "ANTHROPIC_AUTH_TOKEN": "at",
            "DEEPSEEK_API_KEY": "ds-key",
            "OPENAI_API_KEY": "oai-key",
        }
        with patch.dict(os.environ, env, clear=True):
            creds = _load_credentials()
            # anthropic 拿到 AUTH_TOKEN
            assert creds.get("anthropic") == "at"
            # 其他厂商拿到自己 env_key，不被 AUTH_TOKEN 污染
            assert creds.get("deepseek") == "ds-key"
            assert creds.get("openai") == "oai-key"

    def test_build_endpoint_uses_anthropic_base_url_env(self):
        """build_endpoint 优先用 ANTHROPIC_BASE_URL env 而非 defaults。"""
        env = {
            "ANTHROPIC_AUTH_TOKEN": "claude-code-token",
            "ANTHROPIC_BASE_URL": "https://ark.cn-beijing.volces.com/api/coding",
        }
        with patch.dict(os.environ, env, clear=True):
            ep = build_endpoint("anthropic/claude-sonnet-4-20250514")
            assert ep.base_url == "https://ark.cn-beijing.volces.com/api/coding"
            assert ep.api_key == "claude-code-token"

    def test_build_endpoint_explicit_base_url_wins_over_env(self):
        """函数参数 base_url 仍优先于 env。"""
        env = {
            "ANTHROPIC_AUTH_TOKEN": "k",
            "ANTHROPIC_BASE_URL": "https://env-url",
        }
        with patch.dict(os.environ, env, clear=True):
            ep = build_endpoint(
                "anthropic/claude-sonnet-4-20250514",
                base_url="https://explicit-url",
            )
            assert ep.base_url == "https://explicit-url"

    def test_build_endpoint_falls_back_to_default(self):
        """无 env 时用 defaults base_url。"""
        with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "k"}, clear=True):
            os.environ.pop("ANTHROPIC_BASE_URL", None)
            ep = build_endpoint("anthropic/claude-sonnet-4-20250514")
            assert ep.base_url == "https://api.anthropic.com"

    def test_build_endpoint_missing_key_raises(self):
        """无任何 key 时抛错。"""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            with pytest.raises(ValueError, match="anthropic"):
                build_endpoint("anthropic/claude-sonnet-4-20250514")
