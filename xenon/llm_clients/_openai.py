"""OpenAI-compatible provider: chat completion with tools, streaming."""
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

logger = logging.getLogger(__name__)

def _call_openai_compat(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
    *,
    reasoning_effort: str | None = None,
) -> str:
    """OpenAI 兼容格式调用（B12: finish_reason=length 自动续写）。

    A plain ``"继续"`` continuation is safe for prose, but not for the JSON
    and DSML streams used by the ReAct fallback.  A model may restart the
    object after that prompt, leaving the concatenated response invalid.  We
    therefore keep the old behaviour for prose and ask for a *fragment* for
    structured responses; JSON is repaired/validated before it is returned.
    """
    msgs = list(messages)  # 不修改调用方列表
    parts: list[str] = []
    attempts = 0
    current_max_tokens = max_tokens
    while True:
        if reasoning_effort:
            content, finish = _call_openai_compat_once(
                endpoint, msgs, current_max_tokens, temperature, timeout,
                reasoning_effort=reasoning_effort,
            )
        else:
            content, finish = _call_openai_compat_once(
                endpoint, msgs, current_max_tokens, temperature, timeout,
            )
        if content:
            parts.append(content)
        combined = "".join(parts)
        if finish != "length":
            return _finalize_structured_text(combined)
        # Thinking models may spend the entire output budget on hidden
        # reasoning and return no visible content.  Treating that unfinished
        # reasoning as the assistant's answer corrupts JSON/DSML protocols;
        # asking "continue" also starts a new turn instead of completing the
        # original answer.  Retry the unchanged request with a larger budget
        # until visible output appears (or the bounded retry budget is used).
        if not content:
            if attempts < MAX_CONTINUATIONS:
                provider_cap = int(
                    _PROVIDER_DEFAULTS.get(endpoint.provider, {}).get(
                        "max_output_tokens", 8192,
                    )
                )
                expanded = min(
                    provider_cap,
                    max(current_max_tokens * 2, current_max_tokens + 256),
                )
                if expanded > current_max_tokens:
                    attempts += 1
                    logger.info(
                        "API 推理阶段在可见输出前被截断，扩大 max_tokens: %s → %s "
                        "(%s/%s)",
                        current_max_tokens,
                        expanded,
                        attempts,
                        MAX_CONTINUATIONS,
                    )
                    current_max_tokens = expanded
                    continue
            # At the provider cap there is no safe continuation fragment to
            # send back. Fall through to the bounded fail-closed path below.
            attempts = MAX_CONTINUATIONS
        # 被截断 → 追加部分内容为 assistant，再请求"继续"
        if attempts >= MAX_CONTINUATIONS:
            repaired = _finalize_structured_text(combined)
            if repaired != combined and _structured_response_kind(combined) == "json":
                logger.warning(
                    "结构化 JSON 在续写次数耗尽后已修复，避免返回非法协议"
                )
                return repaired
            raise ResponseTruncatedError(
                f"API 响应在 {MAX_CONTINUATIONS} 次续写后仍被截断 "
                f"(finish_reason=length)，内容可能不完整；请增大 max_tokens 或精简输入。"
            )
        attempts += 1
        kind = _structured_response_kind(combined)
        logger.info(
            "API 响应被截断 (finish_reason=length)，自动续写%s…",
            f"（{kind} 协议片段）" if kind else "",
        )
        msgs.append({"role": "assistant", "content": content or ""})
        msgs.append({
            "role": "user",
            "content": _continuation_prompt(kind),
        })


def _structured_response_kind(text: str) -> str | None:
    """Return the response protocol when ``text`` looks structured.

    This intentionally only classifies unquoted protocol markers at the
    beginning (or the explicit DSML/XML markers).  Ordinary prose containing
    an example JSON object keeps the historical continuation behaviour.
    """
    stripped = (text or "").lstrip()
    if stripped.startswith(("{", "[")) or stripped.startswith("```json"):
        return "json"
    # DeepSeek sometimes emits full-width vertical bars in DSML markers.
    lowered = stripped.replace("｜", "|").lower()
    if any(marker in lowered for marker in (
        "<||dsml||tool_calls", "<uses_legacy_tools", "<tool_calls",
    )):
        return "dsml"
    return None


def _continuation_prompt(kind: str | None) -> str:
    """Build a continuation instruction that preserves the active protocol."""
    if kind == "json":
        return (
            "继续输出上一个 JSON 对象被截断的剩余片段。只输出缺失内容，"
            "不要重复已经输出的字符，不要添加说明或新的 JSON 对象。"
        )
    if kind == "dsml":
        return (
            "继续输出上一个 DSML 工具调用协议被截断的剩余片段。只输出缺失的"
            "标签或参数，不要重复已输出内容，不要改用 JSON 或自然语言。"
        )
    return "继续"


def _finalize_structured_text(text: str) -> str:
    """Validate structured text and repair a provider-side hard truncation.

    ``finish_reason`` is occasionally reported as ``stop`` even when the
    provider cut a JSON object at a byte boundary.  Returning a best-effort
    repaired object is safer than handing an invalid protocol to the ReAct
    parser; prose is returned byte-for-byte unchanged.
    """
    kind = _structured_response_kind(text)
    if kind != "json":
        return text
    try:
        json.loads(text, strict=False)
        return text
    except (TypeError, ValueError, json.JSONDecodeError):
        try:
            from xenon.utils.response_adapter import _repair_json

            repaired = _repair_json(text)
            if repaired:
                json.loads(repaired, strict=False)
                logger.warning("结构化 JSON 响应已在客户端修复后返回")
                return repaired
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        return text


def _call_openai_compat_once(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
    *,
    reasoning_effort: str | None = None,
) -> tuple[str, str]:
    """单次 OpenAI 兼容调用，返回 (content, finish_reason)。"""
    url = f"{endpoint.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {endpoint.api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": endpoint.model_name,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    _apply_reasoning_effort(payload, reasoning_effort)
    # R3: 复用 per-provider 长生命 Client（取代每次 with _create_http_client）
    client = _get_pooled_client(endpoint, timeout)
    resp = client.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    # §8.8.1：提取并累加真实 usage（不再丢弃），+ model_id 用于缓存追踪
    _acc_usage(endpoint.provider, data, f"{endpoint.provider}/{endpoint.model_name}")
    msg = data["choices"][0]["message"]
    finish = data["choices"][0].get("finish_reason", "")
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or msg.get("thinking") or ""

    if content:
        logger.debug(f"API 响应: content={content[:300]}")
    elif reasoning:
        logger.debug(f"API 响应: content=空, reasoning_content={reasoning[:300]}")
    else:
        logger.warning(f"API 响应: content 和 reasoning_content 均为空! finish_reason={finish}")

    # 推理模型在正常结束时偶尔只返回 reasoning_content；保留兼容兜底。
    # ``length`` 表示该推理本身尚未完成，绝不能把它冒充最终答案或协议正文。
    if not content and reasoning and finish != "length":
        content = reasoning

    return content, finish




# ── Tool-calling variant ──
def _call_openai_compat_with_tools(
    endpoint: "ModelEndpoint",
    messages: list[dict[str, str]],
    tools: list[dict[str, Any]] | None,
    response_format: dict[str, Any] | None,
    tool_choice: str | dict[str, Any] | None,
    max_tokens: int,
    temperature: float,
    timeout: float,
    *,
    reasoning_effort: str | None = None,
) -> LLMResponse:
    """OpenAI 兼容厂商的原生 FC 调用（单次，不带 B12 续写——FC 场景续写语义复杂，留给上层）。"""
    url = f"{endpoint.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {endpoint.api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": endpoint.model_name,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    _apply_reasoning_effort(payload, reasoning_effort)
    norm_tools = _normalize_openai_tools(tools)
    if norm_tools:
        payload["tools"] = norm_tools
    if response_format:
        payload["response_format"] = response_format
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
        # DeepSeek V4 默认开启思考模式，而服务端不允许思考模式与
        # required/none/指定函数等强制选择同时使用。此时优先保证
        # tool_choice 语义，仅关闭这一次请求的思考模式。
        # DeepSeek V4 keeps the same thinking/tool-choice constraint when it
        # is reached through the official DeepSeek endpoint, Ark's
        # OpenAI-compatible endpoint, or a legacy custom-provider alias.  Do
        # not key this solely on ``endpoint.provider``: credentials created by
        # older Xenon versions commonly use ``custom/`` for Ark models.
        if (
            endpoint.model_name.lower().startswith("deepseek-v4-")
            and tool_choice != "auto"
        ):
            payload.pop("reasoning_effort", None)
            payload["thinking"] = {"type": "disabled"}

    client = _get_pooled_client(endpoint, timeout)
    resp = client.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    # §8.8.1：提取并累加真实 usage（含缓存命中数据）
    _acc_usage(endpoint.provider, data, f"{endpoint.provider}/{endpoint.model_name}")
    choice = data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or msg.get("thinking") or ""
    finish = choice.get("finish_reason", "")
    tool_calls = _parse_openai_tool_calls(msg)
    # Native FC responses cannot be resumed by appending a user "continue":
    # the assistant/tool_call_id envelope must remain one atomic protocol
    # message.  Never expose a truncated tool call to the executor; failing
    # closed lets the engine report/recover the protocol error instead.
    if finish == "length":
        raise ResponseTruncatedError(
            "原生工具调用响应因 finish_reason=length 被截断，"
            "为避免执行不完整的工具参数，已拒绝该响应。"
        )
    return LLMResponse(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
        finish_reason=finish,
        usage=_extract_usage(data, endpoint.provider),
        raw=data,
        provider=endpoint.provider,
        assistant_message=dict(msg),
    )




# ── Streaming variant ──
def _stream_openai_compat(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
    model_id: str,
    *,
    reasoning_effort: str | None = None,
    manifest: dict[str, Any] | None = None,
) -> Generator[str, None, None]:
    """OpenAI 兼容格式流式调用。

    P3-Q1 续 / §8.8.1：机会性提取末尾 chunk 的 ``usage``（部分兼容厂商默认随
    末帧返回；OpenAI 官方需 ``stream_options.include_usage``，此处不强加以避免
    对不支持的厂商触发 400）。提取到则经 usage 回调发出真实 token 用量。
    """
    import time

    url = f"{endpoint.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {endpoint.api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": endpoint.model_name,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    if endpoint.provider == "ark":
        # Ark's OpenAI-compatible streaming API supports an explicit final
        # usage chunk. Request it so /cost and /cache have provider evidence.
        payload["stream_options"] = {"include_usage": True}
    _apply_reasoning_effort(payload, reasoning_effort)
    t0 = time.time()
    usage_data: dict[str, Any] | None = None
    with _create_http_client(timeout=timeout) as client:
        with client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk.get("usage"), dict):
                    usage_data = chunk
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if content:
                    yield content
    if usage_data is not None:
        _emit_usage(model_id, _extract_usage(usage_data, endpoint.provider), time.time() - t0)
        # 发出响应回调（供 CacheTracker 等订阅原始 API 响应）
        _emit_response(model_id, _response_with_manifest(usage_data, manifest))




# ── OpenAI-compatible tool helpers ──
def _normalize_openai_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """OpenAI 兼容厂商直接透传 tools（已是 {type:function, function:{...}} 形态）。"""
    if not tools:
        return None
    return [
        t if t.get("type") else {"type": "function", "function": t}
        for t in tools
    ]


def _openai_to_anthropic_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """把 OpenAI 风格 tools 转为 Anthropic 原生格式 [{name, description, input_schema}]。"""
    if not tools:
        return None
    converted = []
    for t in tools:
        fn = t.get("function", t)  # 兼容裸函数定义
        converted.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or fn.get("input_schema") or {"type": "object", "properties": {}},
        })
    return converted


def _parse_openai_tool_calls(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 OpenAI message.tool_calls 为统一结构。"""
    out = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        args_raw = fn.get("arguments", "{}")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
        except (json.JSONDecodeError, TypeError):
            # 参数非合法 JSON — 保留原始字符串，调用方自行处理
            args = {"_raw": args_raw}
        out.append({
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "arguments": args,
        })
    return out


