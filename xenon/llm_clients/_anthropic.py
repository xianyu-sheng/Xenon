"""Anthropic-native provider: chat completion with tools, streaming."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from xenon.llm_clients._base import (
    LLMResponse, ModelEndpoint, ResponseTruncatedError,
    _PROVIDER_DEFAULTS, _acc_usage, _apply_reasoning_effort,
    _emit_response, _extract_usage, _get_pooled_client,
    _normalize_reasoning_effort, _response_with_manifest,
    compile_prompt,
)
from xenon.llm_clients._openai import _openai_to_anthropic_tools

logger = logging.getLogger(__name__)

def _call_anthropic(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> str:
    """Anthropic 原生 API 格式调用（B12: stop_reason=max_tokens 自动续写）。"""
    msgs = list(messages)  # 不修改调用方列表
    parts: list[str] = []
    attempts = 0
    while True:
        content, stop_reason = _call_anthropic_once(
            endpoint, msgs, max_tokens, temperature, timeout)
        if content:
            parts.append(content)
        if stop_reason != "max_tokens":
            return "".join(parts)
        if attempts >= MAX_CONTINUATIONS:
            raise ResponseTruncatedError(
                f"Anthropic 响应在 {MAX_CONTINUATIONS} 次续写后仍被截断 "
                f"(stop_reason=max_tokens)，内容可能不完整；请增大 max_tokens 或精简输入。"
            )
        attempts += 1
        logger.info("Anthropic 响应被截断 (stop_reason=max_tokens)，自动续写…")
        msgs.append({"role": "assistant", "content": content or ""})
        msgs.append({"role": "user", "content": "继续"})


def _call_anthropic_once(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> tuple[str, str]:
    """单次 Anthropic 调用，返回 (text, stop_reason)。"""
    url = f"{endpoint.base_url}/v1/messages"
    headers = {
        "x-api-key": endpoint.api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    # Anthropic 要求 system 单独传递，并使用 content blocks 表示工具往返。
    system_text, chat_messages = _messages_for_anthropic(messages)

    payload: dict[str, Any] = {
        "model": endpoint.model_name,
        "messages": chat_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system_text:
        payload["system"] = system_text

    # R3: 复用 per-provider 长生命 Client
    client = _get_pooled_client(endpoint, timeout)
    resp = client.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    # §8.8.1：提取并累加真实 usage（Anthropic 用 input/output_tokens）
    _acc_usage(endpoint.provider, data, f"{endpoint.provider}/{endpoint.model_name}")
    # content 是文本块列表；拼接所有 text 块（比仅取 [0] 更鲁棒）
    blocks = data.get("content", []) or []
    text = "".join(
        b.get("text", "") for b in blocks if isinstance(b, dict)
    )
    stop_reason = data.get("stop_reason", "")
    return text, stop_reason


# ── R3: 原生 function-calling 能力（Q2 三层降级前置） ──────


def _parse_anthropic_tool_calls(blocks: list[Any]) -> tuple[str, list[dict[str, Any]], str]:
    """解析 Anthropic content blocks，返回 (text, tool_calls, stop_reason)。

    text = 拼接所有 text 块；tool_calls 来自 tool_use 块。
    """
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            text_parts.append(b.get("text", ""))
        elif b.get("type") == "tool_use":
            tool_calls.append({
                "id": b.get("id", ""),
                "name": b.get("name", ""),
                "arguments": b.get("input") or {},
            })
    return "".join(text_parts), tool_calls, ""


def _messages_for_anthropic(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """把内部 OpenAI 风格历史转换为 Anthropic messages。

    Xenon 将原生工具往返统一保存为 OpenAI 风格，方便 DeepSeek 与其他兼容
    端点原样续轮。模型回退到 Anthropic 时，在边界处转换为 ``tool_use`` /
    ``tool_result`` blocks，避免跨厂商 fallback 因历史格式不兼容而失败。
    """
    system_parts: list[str] = []
    chat_messages: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        if pending_results:
            chat_messages.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if role == "system":
            flush_tool_results()
            system_parts.append(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))
            continue
        if role == "tool":
            pending_results.append({
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id", "")),
                "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
            })
            continue

        flush_tool_results()
        if role == "assistant" and message.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if content:
                blocks.append({"type": "text", "text": str(content)})
            for tool_call in message.get("tool_calls", []):
                function = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {"_raw": arguments}
                blocks.append({
                    "type": "tool_use",
                    "id": str(tool_call.get("id", "")),
                    "name": str(function.get("name", "")),
                    "input": arguments if isinstance(arguments, dict) else {},
                })
            chat_messages.append({"role": "assistant", "content": blocks})
            continue

        chat_messages.append({"role": role, "content": content})

    flush_tool_results()
    return "\n\n".join(part for part in system_parts if part), chat_messages




# ── Tool-calling variant ──
def _call_anthropic_with_tools(
    endpoint: "ModelEndpoint",
    messages: list[dict[str, str]],
    tools: list[dict[str, Any]] | None,
    response_format: dict[str, Any] | None,
    tool_choice: str | dict[str, Any] | None,
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> LLMResponse:
    """Anthropic 原生 tools 调用。

    response_format（OpenAI JSON mode）在 Anthropic 无直接对应，降级为在 system
    末尾追加"以 JSON 输出"提示词——真正的 JSON 解析由 response_adapter 兜底。
    """
    url = f"{endpoint.base_url}/v1/messages"
    headers = {
        "x-api-key": endpoint.api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    system_text, chat_messages = _messages_for_anthropic(messages)

    if response_format and "json" in json.dumps(response_format).lower():
        system_text = (system_text + "\n\n" if system_text else "") + "请严格以合法 JSON 输出，不要包含多余文本。"

    payload: dict[str, Any] = {
        "model": endpoint.model_name,
        "messages": chat_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system_text:
        payload["system"] = system_text
    anthropic_tools = _openai_to_anthropic_tools(tools)
    if anthropic_tools:
        payload["tools"] = anthropic_tools
    if tool_choice is not None:
        # OpenAI: "auto"|"none"|"required"|{type:function,name}
        # Anthropic: {type:"auto"|"any"|"tool", name?}
        if tool_choice == "auto":
            payload["tool_choice"] = {"type": "auto"}
        elif tool_choice == "required":
            payload["tool_choice"] = {"type": "any"}
        elif tool_choice == "none":
            # Anthropic 无 none；不传 tools 即可，这里保留 tools 但不强制
            pass
        elif isinstance(tool_choice, dict):
            payload["tool_choice"] = {"type": "tool", "name": tool_choice.get("function", {}).get("name", "")}
        else:
            payload["tool_choice"] = {"type": "auto"}

    client = _get_pooled_client(endpoint, timeout)
    resp = client.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    _acc_usage(endpoint.provider, data, f"{endpoint.provider}/{endpoint.model_name}")
    blocks = data.get("content", []) or []
    text, tool_calls, _ = _parse_anthropic_tool_calls(blocks)
    # Anthropic stop_reason → OpenAI 风格 finish_reason
    stop = data.get("stop_reason", "")
    finish = "tool_calls" if stop == "tool_use" else ("length" if stop == "max_tokens" else stop or "stop")
    if finish == "length":
        raise ResponseTruncatedError(
            "Anthropic 原生工具调用响应因 stop_reason=max_tokens 被截断，"
            "为避免执行不完整的工具参数，已拒绝该响应。"
        )
    canonical_calls = [
        {
            "id": call.get("id", ""),
            "type": "function",
            "function": {
                "name": call.get("name", ""),
                "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
            },
        }
        for call in tool_calls
    ]
    assistant_message: dict[str, Any] = {"role": "assistant", "content": text}
    if canonical_calls:
        assistant_message["tool_calls"] = canonical_calls
    return LLMResponse(
        content=text,
        tool_calls=tool_calls,
        finish_reason=finish,
        usage=_extract_usage(data, endpoint.provider),
        raw=data,
        provider=endpoint.provider,
        assistant_message=assistant_message,
    )


# ── 流式调用接口 ──────────────────────────────────────────




# ── Streaming variant ──
def _stream_anthropic(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
    model_id: str,
    manifest: dict[str, Any] | None = None,
) -> Generator[str, None, None]:
    """Anthropic 原生格式流式调用。

    P3-Q1 续 / §8.8.1：从 ``message_start`` 取 input_tokens、``message_delta``
    取 output_tokens（末值为最终输出），结束后经 usage 回调发出真实用量。
    """
    import time

    url = f"{endpoint.base_url}/v1/messages"
    headers = {
        "x-api-key": endpoint.api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    system_text, chat_messages = _messages_for_anthropic(messages)

    payload: dict[str, Any] = {
        "model": endpoint.model_name,
        "messages": chat_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    if system_text:
        payload["system"] = system_text

    t0 = time.time()
    input_tokens = 0
    output_tokens = 0
    with _create_http_client(timeout=timeout) as client:
        with client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "message_start":
                    u = (event.get("message") or {}).get("usage") or {}
                    input_tokens = int(u.get("input_tokens", 0) or 0)
                    output_tokens = int(u.get("output_tokens", 0) or 0)
                elif etype == "message_delta":
                    u = event.get("usage") or {}
                    if "output_tokens" in u:
                        output_tokens = int(u.get("output_tokens", 0) or 0)
                elif etype == "content_block_delta":
                    delta = event.get("delta", {})
                    text = delta.get("text")
                    if text:
                        yield text
    if input_tokens or output_tokens:
        _emit_usage(
            model_id,
            LLMUsage(input_tokens, output_tokens, input_tokens + output_tokens),
            time.time() - t0,
        )
        _emit_response(
            model_id,
            _response_with_manifest(
                {"usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }},
                manifest,
            ),
        )
