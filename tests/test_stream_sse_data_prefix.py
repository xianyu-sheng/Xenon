"""Offline SSE framing contracts requested in #437; no provider credentials."""

import json

import httpx
import pytest

from xenon.utils import llm_client
from xenon.utils.provider_events import END, START


class FragmentedBody(httpx.SyncByteStream):
    def __init__(self, content):
        self.content = content

    def __iter__(self):
        for offset in range(0, len(self.content), 3):
            yield self.content[offset:offset + 3]


def install_transport(monkeypatch, body, status=200):
    requests, usage, responses = [], [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status, headers={"Content-Type": "text/event-stream"},
            stream=FragmentedBody(body),
        )

    monkeypatch.setattr(
        llm_client, "_create_http_client",
        lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(handler), trust_env=False,
        ),
    )
    monkeypatch.setattr(llm_client, "_emit_usage", lambda *args: usage.append(args))
    monkeypatch.setattr(
        llm_client, "_emit_response", lambda *args: responses.append(args),
    )
    return requests, usage, responses


def event(space, payload):
    value = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data:{space}{value}\r\n\r\n"


def stream_body(provider, space, truncated=False):
    if provider == "openai":
        payloads = [
            "{malformed-json",
            {"choices": [{"delta": {"content": "你好"}}]},
            {"choices": [{"delta": {}, "finish_reason": "length" if truncated else "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
            "[DONE]",
            {"choices": [{"delta": {"content": "must not follow DONE"}}]},
        ]
    else:
        payloads = [
            "{malformed-json",
            {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "你好"}},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens" if truncated else "end_turn"}, "usage": {"output_tokens": 2}},
            {"type": "message_stop"},
        ]
    return (": comment\r\n\r\n" + "".join(event(space, p) for p in payloads)).encode()


def collect_stream(provider, events):
    return llm_client.chat_completion_stream(
        f"{provider}/fixture-model", [{"role": "user", "content": "fixture"}],
        credentials={provider: "fixture-key"},
        base_url="https://provider.invalid/v1" if provider == "openai" else "https://provider.invalid",
        request_sink=events.append,
    )


PREFIXES = [
    pytest.param(" ", id="spaced"),
    pytest.param(
        "", id="no-space",
        marks=pytest.mark.xfail(
            strict=True,
            reason="#437: current streaming parsers require data: followed by a space; test-only first PR",
        ),
    ),
]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("space", PREFIXES)
def test_content_usage_and_events_preserve_stream_contract(monkeypatch, provider, space):
    requests, usage, responses = install_transport(monkeypatch, stream_body(provider, space))
    events = []
    assert "".join(collect_stream(provider, events)) == "你好"
    assert len(requests) == 1
    expected_path = "/v1/chat/completions" if provider == "openai" else "/v1/messages"
    assert requests[0].url.path == expected_path
    assert json.loads(requests[0].content)["stream"] is True
    assert len(usage) == len(responses) == 1
    assert usage[0][0] == f"{provider}/fixture-model"
    assert (usage[0][1].prompt_tokens, usage[0][1].completion_tokens, usage[0][1].total_tokens) == (3, 2, 5)
    assert [e["kind"] for e in events] == [START, END]
    assert events[0]["provider_attempts"] == events[0]["attempt"] == 1
    assert events[0]["streamed"] is True
    assert events[1]["success"] is True


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("space", PREFIXES)
def test_truncation_is_reported_without_retry(monkeypatch, provider, space):
    requests, usage, _ = install_transport(monkeypatch, stream_body(provider, space, truncated=True))
    events, chunks = [], []
    with pytest.raises(llm_client.ResponseTruncatedError):
        for chunk in collect_stream(provider, events):
            chunks.append(chunk)
    assert "".join(chunks) == "你好"
    assert len(requests) == len(usage) == 1
    assert [e["kind"] for e in events] == [START, END]
    assert events[0]["provider_attempts"] == events[1]["attempt"] == 1
    assert events[1]["success"] is False
    assert events[1]["retrying"] is False
    assert events[1]["reason"] == "ResponseTruncatedError"


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("space", [" ", ""])
def test_malformed_json_is_ignored_without_usage_or_retry(monkeypatch, provider, space):
    body = event(space, "{malformed-json").encode()
    requests, usage, responses = install_transport(monkeypatch, body)
    events = []
    assert list(collect_stream(provider, events)) == []
    assert len(requests) == 1
    assert usage == responses == []
    assert [e["kind"] for e in events] == [START, END]
    assert events[1]["success"] is True


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("status", [429, 503])
def test_http_failures_preserve_single_attempt_reporting(monkeypatch, provider, status):
    requests, usage, _ = install_transport(monkeypatch, b"", status=status)
    events = []
    with pytest.raises(httpx.HTTPStatusError) as raised:
        list(collect_stream(provider, events))
    assert raised.value.response.status_code == status
    assert len(requests) == 1
    assert usage == []
    assert [e["kind"] for e in events] == [START, END]
    assert events[0]["provider_attempts"] == events[1]["attempt"] == 1
    assert events[0]["streamed"] is True
    assert events[1]["success"] is False
    assert events[1]["retrying"] is False
    assert events[1]["reason"] == "HTTPStatusError"
