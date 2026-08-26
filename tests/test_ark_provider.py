"""First-class Volcengine Ark provider contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import yaml

import xenon.repl.provider_registry as providers
import xenon.utils.llm_client as llm
from xenon.repl.model_pool import _infer_capability
from xenon.repl.model_registry import ModelRegistry
from xenon.utils.deepseek_cache import CacheTracker


class _Response:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://ark.example/chat/completions")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=response
            )

    def json(self) -> dict:
        return self.payload


class _PostClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.posts: list[dict] = []

    def post(self, url, *, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return _Response(self.payload)


class _StreamResponse:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self):
        yield from self.lines

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _StreamClient:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.requests: list[dict] = []

    def stream(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        return _StreamResponse(self.lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _ark_endpoint(model: str = "doubao-seed-2-1-pro-260628") -> llm.ModelEndpoint:
    return llm.ModelEndpoint(
        provider="ark",
        model_name=model,
        base_url=providers.ARK_BASE_URL,
        api_key="test-key",
    )


def test_ark_is_a_builtin_provider() -> None:
    ark = providers.PROVIDERS["ark"]
    assert ark.name == "火山方舟 Ark"
    assert ark.base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert ark.env_key == "ARK_API_KEY"
    assert ark.models == providers.ARK_FALLBACK_MODELS


def test_ark_model_discovery_uses_bearer_and_stable_priority(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    for provider in providers.PROVIDERS.values():
        monkeypatch.delenv(provider.env_key, raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    class _GetClient:
        def get(self, url, headers):
            calls.append((url, headers))
            return _Response({"data": [
                {"id": "legacy-model"},
                {"id": "glm-5-2-260617"},
                {
                    "id": "doubao-seed-2-1-pro-260628",
                    "task_type": ["TextGeneration"],
                    "token_limits": {"context_window": 262144},
                },
                {"id": "deepseek-v4-pro-260425"},
                {
                    "id": "doubao-seedream-5-0-pro-260628",
                    "task_type": ["TextToImage"],
                    "modalities": {"output_modalities": ["image"]},
                },
            ]})

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(providers, "_create_http_client", lambda timeout: _GetClient())
    monkeypatch.setattr(providers, "load_credentials", lambda: {"ark": "secret"})

    configured = providers.get_configured_providers()
    ark = next(item for item in configured if item.key == "ark")

    assert ark.models == [
        "doubao-seed-2-1-pro-260628",
        "glm-5-2-260617",
        "deepseek-v4-pro-260425",
        "legacy-model",
    ]
    assert calls == [(
        "https://ark.cn-beijing.volces.com/api/v3/models",
        {"Accept": "application/json", "Authorization": "Bearer secret"},
    )]
    assert providers.get_model_metadata(
        "ark/doubao-seed-2-1-pro-260628"
    )["context_window"] == 262144
    assert "doubao-seedream-5-0-pro-260628" not in ark.models


def test_legacy_custom_ark_key_is_read_without_rewriting(tmp_path: Path) -> None:
    path = tmp_path / "credentials.yaml"
    original = {
        "_custom_providers": {
            "volc": {
                "name": "Volcengine",
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "api_key": "legacy-secret",
            }
        }
    }
    path.write_text(yaml.safe_dump(original), encoding="utf-8")

    loaded = providers.load_credentials(path)

    assert loaded["ark"] == "legacy-secret"
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == original


def test_unrelated_write_does_not_silently_persist_legacy_ark(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "credentials.yaml"
    path.write_text(yaml.safe_dump({
        "_custom_providers": {
            "volc": {"base_url": providers.ARK_BASE_URL, "api_key": "legacy-secret"}
        }
    }), encoding="utf-8")
    monkeypatch.setattr(providers, "_get_credentials_path", lambda: path)

    providers.set_provider_key("openai", "openai-secret")
    stored = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert stored["openai"] == "openai-secret"
    assert "ark" not in stored
    assert stored["_custom_providers"]["volc"]["api_key"] == "legacy-secret"


def test_remove_ark_key_removes_legacy_ark_but_preserves_other_custom(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "credentials.yaml"
    path.write_text(yaml.safe_dump({
        "ark": "explicit-secret",
        "_custom_providers": {
            "volc": {"base_url": providers.ARK_BASE_URL, "api_key": "legacy-secret"},
            "other": {"base_url": "https://example.com/v1", "api_key": "other-secret"},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(providers, "_get_credentials_path", lambda: path)

    providers.remove_provider_key("ark")
    stored = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert "ark" not in stored
    assert "volc" not in stored["_custom_providers"]
    assert stored["_custom_providers"]["other"]["api_key"] == "other-secret"


def test_ambiguous_legacy_ark_keys_are_not_guessed() -> None:
    data = {"_custom_providers": {
        "one": {"base_url": providers.ARK_BASE_URL, "api_key": "one"},
        "two": {"base_url": providers.ARK_BASE_URL, "api_key": "two"},
    }}
    assert providers._legacy_ark_api_key(data) == ""
    assert llm._legacy_ark_api_key(data) == ""


def test_build_ark_endpoint_uses_env_and_allows_base_override(monkeypatch) -> None:
    monkeypatch.setattr(llm, "_CREDENTIALS_PATH", Path("/does/not/exist"))
    monkeypatch.setenv("ARK_API_KEY", "ark-env-secret")
    monkeypatch.setenv("ARK_BASE_URL", "https://ark-proxy.example/api/v3")

    endpoint = llm.build_endpoint("ark/glm-5-2-260617")

    assert endpoint.provider == "ark"
    assert endpoint.model_name == "glm-5-2-260617"
    assert endpoint.api_key == "ark-env-secret"
    assert endpoint.base_url == "https://ark-proxy.example/api/v3"


def test_ark_native_tool_call_and_cache_usage(monkeypatch) -> None:
    payload = {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_ark_1",
                    "type": "function",
                    "function": {"name": "weather", "arguments": '{"city":"苏州"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 8,
            "total_tokens": 108,
            "prompt_tokens_details": {"cached_tokens": 72},
        },
    }
    client = _PostClient(payload)
    monkeypatch.setattr(llm, "build_endpoint", lambda *args, **kwargs: _ark_endpoint())
    monkeypatch.setattr(llm, "_get_pooled_client", lambda endpoint, timeout: client)
    tracker = CacheTracker()
    try:
        response = llm.chat_completion_with_tools(
            "ark/doubao-seed-2-1-pro-260628",
            [{"role": "user", "content": "苏州天气"}],
            tools=[{"name": "weather", "parameters": {"type": "object"}}],
        )
        snapshot = tracker.model_snapshot("ark/doubao-seed-2-1-pro-260628")
    finally:
        tracker.close()

    assert response.provider == "ark"
    assert response.tool_calls == [{
        "id": "call_ark_1", "name": "weather", "arguments": {"city": "苏州"}
    }]
    assert response.usage.cache_hit_tokens == 72
    assert response.usage.cache_miss_tokens == 28
    assert snapshot["cache_hit_tokens"] == 72
    assert snapshot["cache_miss_tokens"] == 28
    assert snapshot["cache_field_coverage"] == 1.0
    sent = client.posts[0]
    assert sent["url"] == f"{providers.ARK_BASE_URL}/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer test-key"
    assert sent["json"]["tools"][0]["function"]["name"] == "weather"


def test_ark_stream_requests_and_emits_final_usage(monkeypatch) -> None:
    usage_chunk = {
        "choices": [],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 2,
            "total_tokens": 52,
            "prompt_tokens_details": {"cached_tokens": 40},
        },
    }
    client = _StreamClient([
        'data: {"choices":[{"delta":{"content":"你"}}]}',
        'data: {"choices":[{"delta":{"content":"好"}}]}',
        f"data: {json.dumps(usage_chunk)}",
        "data: [DONE]",
    ])
    seen: list[tuple[str, llm.LLMUsage]] = []
    unsubscribe = llm.register_usage_callback(
        lambda model, usage, latency: seen.append((model, usage))
    )
    monkeypatch.setattr(llm, "build_endpoint", lambda *args, **kwargs: _ark_endpoint())
    monkeypatch.setattr(llm, "_create_http_client", lambda timeout: client)
    try:
        chunks = list(llm.chat_completion_stream(
            "ark/doubao-seed-2-1-pro-260628",
            [{"role": "user", "content": "你好"}],
        ))
    finally:
        unsubscribe()

    assert chunks == ["你", "好"]
    assert client.requests[0]["json"]["stream_options"] == {"include_usage": True}
    assert seen[0][0] == "ark/doubao-seed-2-1-pro-260628"
    assert seen[0][1].cache_hit_tokens == 40
    assert seen[0][1].cache_miss_tokens == 10


def test_ark_context_windows_reach_registry_and_pool() -> None:
    registry = ModelRegistry()
    pro = registry.add_model("ark/deepseek-v4-pro-260425", "ark-pro")
    doubao = registry.add_model("ark/doubao-seed-2-1-pro-260628", "doubao")

    assert pro.context_window == 1_048_576
    assert doubao.context_window == 262_144
    assert _infer_capability(pro.model_id).context_window == 1_048_576
    assert _infer_capability(doubao.model_id).context_window == 262_144


def test_ark_http_error_classification_matches_failover_contract() -> None:
    from xenon.engine.base import BaseEngine
    from xenon.repl.repl import REPL

    def error(status: int) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", f"{providers.ARK_BASE_URL}/chat/completions")
        response = httpx.Response(status, request=request)
        return httpx.HTTPStatusError(str(status), request=request, response=response)

    assert BaseEngine._is_transient_error(error(429)) is True
    assert BaseEngine._is_transient_error(error(503)) is True
    assert BaseEngine._is_transient_error(error(401)) is False
    assert REPL._is_terminal_model_error(error(401)) is True
    assert REPL._is_terminal_model_error(error(404)) is True
