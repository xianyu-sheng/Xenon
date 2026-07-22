"""Explicit five-tier prompt compiler and deterministic tool contract tests."""

from __future__ import annotations

import copy

import xenon.utils.llm_client as llm_client
from xenon.utils.cache_telemetry import build_prompt_manifest
from xenon.utils.llm_client import LLMResponse, ModelEndpoint
from xenon.utils.prompt_compiler import CacheTier, compile_prompt, compile_tools


def test_compiler_classifies_five_tiers_without_reordering() -> None:
    messages = [
        {"role": "system", "content": "fixed"},
        {"role": "system", "content": "project"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "system", "content": "retrieved memory"},
        {"role": "user", "content": "current question"},
    ]
    original = copy.deepcopy(messages)

    compiled = compile_prompt(messages)

    assert compiled.messages == original
    assert messages == original
    assert [segment.tier for segment in compiled.segments] == [
        CacheTier.STATIC,
        CacheTier.SESSION_STABLE,
        CacheTier.HISTORY,
        CacheTier.HISTORY,
        CacheTier.VOLATILE,
        CacheTier.CURRENT,
    ]
    assert compiled.stable_prefix_messages == 2


def test_native_tool_protocol_messages_are_preserved() -> None:
    messages = [
        {"role": "system", "content": "fixed"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "reason",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"x"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]
    assert compile_prompt(messages).messages == messages


def test_tool_order_and_mapping_order_are_deterministic() -> None:
    tool_b = {
        "function": {
            "parameters": {"properties": {"z": {"type": "string"}}, "type": "object"},
            "name": "zeta",
        },
        "type": "function",
    }
    tool_a = {
        "type": "function",
        "function": {"name": "alpha", "parameters": {"type": "object", "properties": {}}},
    }

    first = compile_tools([tool_b, tool_a])
    second = compile_tools([copy.deepcopy(tool_a), copy.deepcopy(tool_b)])
    assert first == second
    assert [tool["function"]["name"] for tool in first or []] == ["alpha", "zeta"]


def test_tool_registry_order_does_not_split_manifest_family() -> None:
    messages = [{"role": "system", "content": "fixed"}, {"role": "user", "content": "q"}]
    tools = [
        {"type": "function", "function": {"name": "zeta", "parameters": {}}},
        {"type": "function", "function": {"name": "alpha", "parameters": {}}},
    ]
    first = compile_prompt(messages, tools=tools)
    second = compile_prompt(messages, tools=list(reversed(tools)))
    manifest_a = build_prompt_manifest(
        "deepseek-v4-flash",
        first.messages,
        tools=first.tools,
        prompt_layout=first.layout(),
    )
    manifest_b = build_prompt_manifest(
        "deepseek-v4-flash",
        second.messages,
        tools=second.tools,
        prompt_layout=second.layout(),
    )
    assert manifest_a.cache_family == manifest_b.cache_family
    assert manifest_a.tool_schema_hash == manifest_b.tool_schema_hash


def test_layout_drives_manifest_cacheable_estimate() -> None:
    messages = [
        {"role": "system", "content": "fixed instruction"},
        {"role": "user", "content": "history"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "current"},
    ]
    compiled = compile_prompt(messages)
    manifest = build_prompt_manifest(
        "deepseek-v4-flash",
        compiled.messages,
        prompt_layout=compiled.layout(),
    )

    assert manifest.expected_cacheable_tokens == compiled.expected_cacheable_tokens
    assert manifest.tier_token_estimates == compiled.layout()["tier_token_estimates"]


def test_dynamic_leading_system_is_reported_but_not_rewritten() -> None:
    messages = [
        {"role": "system", "content": "Current time: 2026-07-22 18:30:00"},
        {"role": "user", "content": "q"},
    ]
    compiled = compile_prompt(messages)

    assert compiled.messages == messages
    assert compiled.warnings == ("dynamic_stable_system:0",)
    manifest = build_prompt_manifest(
        "deepseek-v4-flash",
        compiled.messages,
        prompt_layout=compiled.layout(),
    )
    assert manifest.compiler_warnings == ["dynamic_stable_system:0"]


def test_public_text_client_runs_through_compiler(monkeypatch) -> None:
    captured: dict = {}
    endpoint = ModelEndpoint("deepseek", "deepseek-v4-flash", "https://example.test", "key")
    monkeypatch.setattr(llm_client, "build_endpoint", lambda *args, **kwargs: endpoint)

    def fake_call(_endpoint, messages, max_tokens, temperature, timeout):
        captured["messages"] = messages
        return "ok"

    monkeypatch.setattr(llm_client, "_call_openai_compat", fake_call)
    messages = [{"role": "system", "content": "fixed"}, {"role": "user", "content": "q"}]
    assert llm_client.chat_completion("deepseek-v4-flash", messages) == "ok"
    assert captured["messages"] == messages
    assert llm_client._usage_tl.cache_manifest["tier_token_estimates"]["static"] > 0


def test_public_tool_client_sends_canonical_tool_order(monkeypatch) -> None:
    captured: dict = {}
    endpoint = ModelEndpoint("deepseek", "deepseek-v4-flash", "https://example.test", "key")
    monkeypatch.setattr(llm_client, "build_endpoint", lambda *args, **kwargs: endpoint)

    def fake_call(
        _endpoint,
        messages,
        tools,
        response_format,
        tool_choice,
        max_tokens,
        temperature,
        timeout,
    ):
        captured["tools"] = tools
        return LLMResponse(content="ok")

    monkeypatch.setattr(llm_client, "_call_openai_compat_with_tools", fake_call)
    tools = [
        {"type": "function", "function": {"name": "zeta", "parameters": {}}},
        {"type": "function", "function": {"name": "alpha", "parameters": {}}},
    ]
    response = llm_client.chat_completion_with_tools(
        "deepseek-v4-flash",
        [{"role": "user", "content": "q"}],
        tools=tools,
    )
    assert response.content == "ok"
    assert [tool["function"]["name"] for tool in captured["tools"]] == ["alpha", "zeta"]
