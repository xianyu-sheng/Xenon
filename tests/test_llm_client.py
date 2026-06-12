"""LLM client endpoint configuration tests."""

from __future__ import annotations


def test_build_endpoint_uses_structured_base_url():
    from omniagent.utils.llm_client import build_endpoint

    endpoint = build_endpoint(
        "openai/gpt-5.5",
        credentials={
            "openai": {
                "api_key": "sk-relay",
                "base_url": "https://codex.gogogpt.net",
            },
        },
    )

    assert endpoint.provider == "openai"
    assert endpoint.model_name == "gpt-5.5"
    assert endpoint.api_key == "sk-relay"
    assert endpoint.base_url == "https://codex.gogogpt.net"


def test_build_endpoint_still_accepts_legacy_string_credentials():
    from omniagent.utils.llm_client import build_endpoint

    endpoint = build_endpoint(
        "openai/gpt-4o",
        credentials={"openai": "sk-test"},
    )

    assert endpoint.api_key == "sk-test"
    assert endpoint.base_url == "https://api.openai.com/v1"


def test_openai_compat_url_adds_v1_for_relay_root():
    from omniagent.utils.llm_client import ModelEndpoint, _openai_compat_url

    endpoint = ModelEndpoint(
        provider="openai",
        model_name="gpt-5.5",
        base_url="https://codex.gogogpt.net",
        api_key="sk-relay",
    )

    assert _openai_compat_url(endpoint, "chat/completions") == (
        "https://codex.gogogpt.net/v1/chat/completions"
    )


def test_openai_compat_url_keeps_explicit_v1_base():
    from omniagent.utils.llm_client import ModelEndpoint, _openai_compat_url

    endpoint = ModelEndpoint(
        provider="openai",
        model_name="gpt-5.5",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
    )

    assert _openai_compat_url(endpoint, "chat/completions") == (
        "https://api.openai.com/v1/chat/completions"
    )
