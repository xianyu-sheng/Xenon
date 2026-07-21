"""R3 验收：llm_client 原生 function-calling 能力（Q2 三层降级前置）。

- LLMResponse 结构化返回（content + tool_calls + finish_reason）；
- OpenAI 兼容厂商：tools/response_format/tool_choice 直接透传，解析 message.tool_calls；
- Anthropic：tools 转原生格式，解析 tool_use 块，tool_choice 映射，response_format 降级为 system 提示；
- per-provider 长生命 httpx Client 池：同 endpoint 复用、close_clients 清空。
"""
import xenon.utils.llm_client as llm
from xenon.utils.llm_client import (
    LLMResponse,
    ModelEndpoint,
    _normalize_openai_tools,
    _openai_to_anthropic_tools,
    _parse_anthropic_tool_calls,
    _parse_openai_tool_calls,
    chat_completion_with_tools,
)


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=httpx.Request("POST", "http://x"), response=httpx.Response(self.status_code, request=httpx.Request("POST", "http://x")))

    def json(self):
        return self._payload


class _FakeClient:
    """记录最后一次 post 的 url/json/headers，返回预设 payload。"""

    def __init__(self):
        self.posts = []
        self.is_closed = False

    def post(self, url, *, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(self._next_payload())

    def _next_payload(self):
        payload = self._payloads.pop(0) if getattr(self, "_payloads", None) else self._payload
        return payload

    def set_payload(self, payload):
        self._payload = payload

    def set_payloads(self, payloads):
        self._payloads = list(payloads)

    def close(self):
        self.is_closed = True


def _endpoint(provider, base_url="https://api.example.com/v1"):
    return ModelEndpoint(provider=provider, model_name="m", base_url=base_url, api_key="k")


# ── 纯函数：工具格式转换与解析 ──────────────────────────────
class TestToolConversion:
    def test_llmresponse_defaults(self):
        r = LLMResponse()
        assert r.content == ""
        assert r.tool_calls == []
        assert r.has_tool_calls is False

    def test_llmresponse_has_tool_calls(self):
        r = LLMResponse(tool_calls=[{"name": "x"}])
        assert r.has_tool_calls is True

    def test_normalize_openai_tools_wraps_bare(self):
        tools = [{"name": "echo", "description": "d", "parameters": {}}]
        norm = _normalize_openai_tools(tools)
        assert norm[0]["type"] == "function"
        assert norm[0]["function"]["name"] == "echo"

    def test_normalize_passthrough_when_already_typed(self):
        tools = [{"type": "function", "function": {"name": "echo"}}]
        norm = _normalize_openai_tools(tools)
        assert norm[0]["function"]["name"] == "echo"

    def test_openai_to_anthropic_tools(self):
        tools = [{"type": "function", "function": {"name": "echo", "description": "d", "parameters": {"type": "object"}}}]
        conv = _openai_to_anthropic_tools(tools)
        assert conv[0] == {"name": "echo", "description": "d", "input_schema": {"type": "object"}}

    def test_parse_openai_tool_calls_valid_json(self):
        msg = {"tool_calls": [
            {"id": "c1", "function": {"name": "echo", "arguments": '{"text":"hi"}'}},
        ]}
        out = _parse_openai_tool_calls(msg)
        assert out == [{"id": "c1", "name": "echo", "arguments": {"text": "hi"}}]

    def test_parse_openai_tool_calls_invalid_json_fallback(self):
        msg = {"tool_calls": [{"id": "c1", "function": {"name": "echo", "arguments": "not-json"}}]}
        out = _parse_openai_tool_calls(msg)
        assert out[0]["arguments"] == {"_raw": "not-json"}

    def test_parse_anthropic_tool_calls(self):
        blocks = [
            {"type": "text", "text": "thinking..."},
            {"type": "tool_use", "id": "t1", "name": "echo", "input": {"text": "hi"}},
        ]
        text, tcs, _ = _parse_anthropic_tool_calls(blocks)
        assert text == "thinking..."
        assert tcs == [{"id": "t1", "name": "echo", "arguments": {"text": "hi"}}]


# ── chat_completion_with_tools：OpenAI 路径 ─────────────────
class TestOpenaiFCPath:
    def test_openai_returns_structured_tool_calls(self, monkeypatch):
        fake = _FakeClient()
        fake.set_payload({
            "choices": [{
                "message": {
                    "content": None,
                    "reasoning_content": "I should call echo.",
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "echo", "arguments": '{"text":"hi"}'}}],
                },
                "finish_reason": "tool_calls",
            }],
        })
        monkeypatch.setattr(llm, "_get_pooled_client", lambda endpoint, timeout=120: fake)
        monkeypatch.setattr(llm, "build_endpoint", lambda mid, c=None, b=None: _endpoint("openai"))

        resp = chat_completion_with_tools(
            "openai/gpt-4o", [{"role": "user", "content": "echo hi"}],
            tools=[{"type": "function", "function": {"name": "echo", "parameters": {}}}],
            tool_choice="auto",
            reasoning_effort="high",
        )
        assert resp.has_tool_calls
        assert resp.tool_calls[0]["name"] == "echo"
        assert resp.finish_reason == "tool_calls"
        assert resp.reasoning_content == "I should call echo."
        assert resp.assistant_message["reasoning_content"] == "I should call echo."
        # 透传到 payload
        sent = fake.posts[0]["json"]
        assert sent["tools"][0]["function"]["name"] == "echo"
        assert sent["tool_choice"] == "auto"
        assert sent["reasoning_effort"] == "high"

    def test_openai_response_format_passed_through(self, monkeypatch):
        fake = _FakeClient()
        fake.set_payload({"choices": [{"message": {"content": '{"k":1}'}, "finish_reason": "stop"}]})
        monkeypatch.setattr(llm, "_get_pooled_client", lambda endpoint, timeout=120: fake)
        monkeypatch.setattr(llm, "build_endpoint", lambda mid, c=None, b=None: _endpoint("openai"))

        resp = chat_completion_with_tools(
            "openai/gpt-4o", [{"role": "user", "content": "x"}],
            response_format={"type": "json_object"},
        )
        assert resp.content == '{"k":1}'
        assert fake.posts[0]["json"]["response_format"] == {"type": "json_object"}

    def test_deepseek_v4_forced_tool_choice_disables_thinking(self, monkeypatch):
        fake = _FakeClient()
        fake.set_payload({
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        })
        endpoint = ModelEndpoint(
            provider="deepseek",
            model_name="deepseek-v4-flash",
            base_url="https://api.deepseek.com/v1",
            api_key="k",
        )
        monkeypatch.setattr(llm, "_get_pooled_client", lambda endpoint, timeout=120: fake)
        monkeypatch.setattr(llm, "build_endpoint", lambda mid, c=None, b=None: endpoint)

        chat_completion_with_tools(
            "deepseek/deepseek-v4-flash",
            [{"role": "user", "content": "call echo"}],
            tools=[{"type": "function", "function": {"name": "echo", "parameters": {}}}],
            tool_choice="required",
            reasoning_effort="max",
        )

        sent = fake.posts[0]["json"]
        assert sent["tool_choice"] == "required"
        assert sent["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in sent

    def test_plain_deepseek_request_passes_reasoning_effort(self, monkeypatch):
        fake = _FakeClient()
        fake.set_payload({
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        })
        endpoint = ModelEndpoint(
            provider="deepseek",
            model_name="deepseek-v4-pro",
            base_url="https://api.deepseek.com/v1",
            api_key="k",
        )
        monkeypatch.setattr(llm, "_get_pooled_client", lambda endpoint, timeout=120: fake)
        monkeypatch.setattr(llm, "build_endpoint", lambda mid, c=None, b=None: endpoint)

        result = llm.chat_completion(
            "deepseek/deepseek-v4-pro",
            [{"role": "user", "content": "reason carefully"}],
            reasoning_effort="max",
        )

        assert result == "ok"
        assert fake.posts[0]["json"]["reasoning_effort"] == "max"

    def test_invalid_reasoning_effort_rejected_before_request(self, monkeypatch):
        monkeypatch.setattr(
            llm,
            "build_endpoint",
            lambda mid, c=None, b=None: _endpoint("openai"),
        )
        import pytest

        with pytest.raises(ValueError, match="reasoning_effort"):
            llm.chat_completion(
                "openai/gpt-4o",
                [{"role": "user", "content": "hi"}],
                reasoning_effort="extreme",
            )

    def test_no_tools_degrades_to_text(self, monkeypatch):
        fake = _FakeClient()
        fake.set_payload({"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]})
        monkeypatch.setattr(llm, "_get_pooled_client", lambda endpoint, timeout=120: fake)
        monkeypatch.setattr(llm, "build_endpoint", lambda mid, c=None, b=None: _endpoint("openai"))

        resp = chat_completion_with_tools("openai/gpt-4o", [{"role": "user", "content": "hi"}])
        assert resp.content == "hello"
        assert resp.tool_calls == []
        assert "tools" not in fake.posts[0]["json"]


# ── chat_completion_with_tools：Anthropic 路径 ──────────────
class TestAnthropicFCPath:
    def test_anthropic_tool_use_parsed(self, monkeypatch):
        fake = _FakeClient()
        fake.set_payload({
            "content": [
                {"type": "text", "text": "calling echo"},
                {"type": "tool_use", "id": "t1", "name": "echo", "input": {"text": "hi"}},
            ],
            "stop_reason": "tool_use",
        })
        monkeypatch.setattr(llm, "_get_pooled_client", lambda endpoint, timeout=120: fake)
        monkeypatch.setattr(llm, "build_endpoint", lambda mid, c=None, b=None: _endpoint("anthropic", "https://api.anthropic.com"))

        resp = chat_completion_with_tools(
            "anthropic/claude", [{"role": "user", "content": "echo hi"}],
            tools=[{"type": "function", "function": {"name": "echo", "parameters": {}}}],
            tool_choice="auto",
        )
        assert resp.has_tool_calls
        assert resp.tool_calls[0]["name"] == "echo"
        assert resp.finish_reason == "tool_calls"
        sent = fake.posts[0]["json"]
        assert sent["tools"][0]["name"] == "echo"  # 已转原生格式
        assert sent["tool_choice"] == {"type": "auto"}

    def test_anthropic_required_maps_to_any(self, monkeypatch):
        fake = _FakeClient()
        fake.set_payload({"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"})
        monkeypatch.setattr(llm, "_get_pooled_client", lambda endpoint, timeout=120: fake)
        monkeypatch.setattr(llm, "build_endpoint", lambda mid, c=None, b=None: _endpoint("anthropic", "https://api.anthropic.com"))

        chat_completion_with_tools(
            "anthropic/claude", [{"role": "user", "content": "x"}],
            tools=[{"type": "function", "function": {"name": "echo"}}],
            tool_choice="required",
        )
        assert fake.posts[0]["json"]["tool_choice"] == {"type": "any"}

    def test_anthropic_response_format_appends_json_hint(self, monkeypatch):
        fake = _FakeClient()
        fake.set_payload({"content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn"})
        monkeypatch.setattr(llm, "_get_pooled_client", lambda endpoint, timeout=120: fake)
        monkeypatch.setattr(llm, "build_endpoint", lambda mid, c=None, b=None: _endpoint("anthropic", "https://api.anthropic.com"))

        chat_completion_with_tools(
            "anthropic/claude",
            [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "x"}],
            response_format={"type": "json_object"},
        )
        sent = fake.posts[0]["json"]
        assert "JSON" in sent["system"]
        assert "你是助手" in sent["system"]


# ── per-provider Client 池 ─────────────────────────────────
class TestClientPool:
    def test_same_endpoint_reuses_client(self):
        llm.close_clients()
        ep = _endpoint("openai")
        c1 = llm._get_pooled_client(ep)
        c2 = llm._get_pooled_client(ep)
        assert c1 is c2

    def test_different_endpoints_different_clients(self):
        llm.close_clients()
        c1 = llm._get_pooled_client(_endpoint("openai", "https://a/v1"))
        c2 = llm._get_pooled_client(_endpoint("anthropic", "https://b"))
        assert c1 is not c2

    def test_close_clients_clears_pool(self):
        ep = _endpoint("openai")
        llm._get_pooled_client(ep)
        llm.close_clients()
        assert llm._CLIENT_POOL == {}
