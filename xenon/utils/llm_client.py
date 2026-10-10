"""
LLM Client — 多厂商统一调用适配器。

职责：
1. 从全局凭证文件 (~/.xenon/credentials.yaml) 加载 API Key。
2. 根据 model_id 前缀 (如 "anthropic/claude-3-5-sonnet") 路由到对应厂商的 HTTP 端点。
3. 封装统一的 chat completion 调用，返回纯文本。
"""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from typing import Any

import logging
import httpx

from xenon.utils.partial_response import (
    PartialContent,
    PartialResponseError,
)
from xenon.utils.prompt_compiler import (
    canonicalize_request_value,
    compile_prompt,
)

# Phase 3: 检查点管理器
# 延迟导入，避免循环依赖
def _get_checkpoint_manager():
    """延迟导入检查点管理器"""
    from xenon.engine.checkpoint_manager import CheckpointManager
    return CheckpointManager

logger = logging.getLogger(__name__)

from xenon.utils.llm_errors import ResponseTruncatedError  # noqa: F401,E402
from xenon.utils.llm_usage import _emit_response, _usage_tl  # noqa: F401,E402
from xenon.utils.llm_transport import (  # noqa: F401,E402
    ModelEndpoint,
    _CLIENT_LOCK,
    _CLIENT_POOL,
    _CREDENTIALS_PATH,
    _build_proxy_config,
    _client_pool_key,
    _create_http_client,
    _get_pooled_client,
    _legacy_ark_api_key,
    _load_credentials,
    _load_custom_provider_config,
    build_endpoint,
    close_clients,
    parse_model_id,
)
from xenon.utils.llm_usage import (  # noqa: F401,E402
    LLMResponse,
    LLMUsage,
    UsageTracker,
    _USAGE_CALLBACKS,
    _acc_usage,
    _emit_usage,
    _extract_usage,
    _prepare_cache_lane,
    _response_with_manifest,
    _set_cache_manifest,
    register_response_callback,
    register_usage_callback,
)



# B12: finish_reason=length（OpenAI 兼容）/ stop_reason=max_tokens（Anthropic）
# 时自动续写的最大次数；耗尽后抛 ResponseTruncatedError，而不是仅 logger.warning
# 后静默返回被截断的内容。可配置：XENON_MAX_CONTINUATIONS（默认 3）。
def _max_continuations() -> int:
    try:
        return max(1, int(os.environ.get("XENON_MAX_CONTINUATIONS", "3")))
    except ValueError:
        return 3


MAX_CONTINUATIONS = _max_continuations()


def _mark_truncated(text: str) -> str:
    """截断修复后诚实标注：在 final_answer 尾部追加可见标记。

    B12 续写次数耗尽后 JSON 被客户端修复——修复成功意味着内容被截断过，
    不能再静默当作完整输出交给用户。"""

    marker = "\n\n⚠️ [本回复因输出长度限制被截断，内容不完整，可要求继续或细化问题]"
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("final_answer"), str):
            data["final_answer"] = data["final_answer"] + marker
            return json.dumps(data, ensure_ascii=False)
    except Exception:  # noqa: BLE001 — 非 JSON 文本直接追加
        pass
    return text + marker
_REASONING_EFFORTS = frozenset({"low", "medium", "high", "max", "off"})


def _retry_delay(error: httpx.HTTPStatusError, attempt: int) -> float:
    """Return a bounded provider-directed retry delay when available."""
    try:
        headers = error.response.headers
        retry_after = headers.get("retry-after")
        if retry_after is None:
            # Plain mappings used by adapters/tests are not necessarily
            # case-insensitive like ``httpx.Headers``.
            retry_after = headers.get("Retry-After")
    except (AttributeError, TypeError):
        retry_after = None
    if retry_after is not None:
        try:
            return min(max(float(retry_after), 0.0), 30.0)
        except (TypeError, ValueError):
            pass
    return min(float(2**attempt), 30.0)


def _normalize_reasoning_effort(value: str | None) -> str | None:
    """Validate an OpenAI-compatible reasoning effort value."""
    if value is None or not str(value).strip():
        return None
    normalized = str(value).strip().lower()
    if normalized not in _REASONING_EFFORTS:
        allowed = ", ".join(sorted(_REASONING_EFFORTS))
        raise ValueError(f"reasoning_effort 必须是 {allowed} 之一")
    return normalized


def _apply_reasoning_effort(
    payload: dict[str, Any],
    reasoning_effort: str | None,
) -> None:
    """Add reasoning_effort only when the caller explicitly configured it.

    ``off``（DeepSeek V4 官方 API 实测）表示关闭思考模式：默认思考模式会
    把整个 max_tokens 预算花在 reasoning 上（500/500 tokens 全被推理吃掉，
    content 为空且 finish_reason=length），SWE-bench 评测直接截断失败。
    关闭后走 ``thinking: {\"type\": \"disabled\"}``（DeepSeek V4 协议），
    直接输出可见内容。其他值照旧走 ``reasoning_effort`` 字段。
    """
    normalized = _normalize_reasoning_effort(reasoning_effort)
    if normalized == "off":
        payload["thinking"] = {"type": "disabled"}
    elif normalized:
        payload["reasoning_effort"] = normalized


def chat_completion(
    model_id: str,
    messages: list[dict[str, str]],
    *,
    credentials: dict[str, str] | None = None,
    base_url: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    reasoning_effort: str | None = None,
    cache_context: dict[str, Any] | None = None,
    cache_lane_registry: Any = None,
    timeout: float = 120.0,
    max_retries: int = 3,
) -> str:
    """
    统一的 chat completion 调用（带重试）。

    根据 provider 自动选择正确的 API 格式（OpenAI 兼容 / Anthropic 原生）。
    返回模型的文本回复。

    重试策略:
    - 429 限流: 指数退避重试（1s, 2s, 4s）
    - 5xx 服务端错误: 重试后跳下一个模型
    - 网络超时: 重试 1 次
    """
    import time

    endpoint = build_endpoint(model_id, credentials, base_url)
    compiled = compile_prompt(messages)
    messages = compiled.messages
    cache_context = _prepare_cache_lane(
        cache_lane_registry,
        model_id,
        messages,
        cache_context=cache_context,
    )
    _set_cache_manifest(
        model_id,
        messages,
        cache_context=cache_context,
        prompt_layout=compiled.layout(),
    )
    reasoning_effort = _normalize_reasoning_effort(reasoning_effort)
    # The model configuration is the output-budget authority.  Xenon used to
    # clamp this value by provider name (for example OpenAI-compatible relays
    # to 16K), which silently overrode the real model capability and could cut
    # long answers.  Pass the configured value through unchanged; an upstream
    # capability error remains explicit and can be fixed in that model entry.
    last_error = None

    # §8.8.1：为本调用初始化 usage 累加器（线程局部，跨续写次数累加）
    _usage_tl.usage_acc = LLMUsage()
    t0 = time.monotonic()

    # ``max_retries=0`` means one request without retry (used by probes), not
    # zero requests followed by ``raise None``.
    attempt_count = max(1, max_retries)
    for attempt in range(attempt_count):
        try:
            if endpoint.provider == "anthropic":
                text = _call_anthropic(
                    endpoint, messages, max_tokens, temperature, timeout
                )
            else:
                if reasoning_effort:
                    text = _call_openai_compat(
                        endpoint,
                        messages,
                        max_tokens,
                        temperature,
                        timeout,
                        reasoning_effort=reasoning_effort,
                    )
                else:
                    text = _call_openai_compat(
                        endpoint,
                        messages,
                        max_tokens,
                        temperature,
                        timeout,
                    )
            # 成功：发出 (model_id, 累计 usage, 延迟) 供 UsageTracker 等订阅
            latency = time.monotonic() - t0
            _emit_usage(model_id, getattr(_usage_tl, "usage_acc", LLMUsage()), latency)
            return text

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 429:
                last_error = e
                if attempt + 1 >= attempt_count:
                    break
                # Prefer the provider's rolling-window guidance when present.
                wait = _retry_delay(e, attempt)
                logger.warning(
                    f"[{model_id}] 429 限流，等待 {wait:g}s 后重试 (第 {attempt + 1}/{attempt_count} 次)"
                )
                time.sleep(wait)
            elif 500 <= status < 600:
                last_error = e
                if attempt + 1 >= attempt_count:
                    break
                # 服务端错误 — 指数退避重试
                wait = _retry_delay(e, attempt)
                logger.warning(
                    f"[{model_id}] {status} 服务端错误，等待 {wait:g}s 后重试 (第 {attempt + 1}/{attempt_count} 次)"
                )
                time.sleep(wait)
            else:
                # 其他 HTTP 错误 — 不重试
                raise

        except (
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.RemoteProtocolError,  # "Server disconnected without sending a response"
            httpx.WriteError,  # 写入连接失败
            httpx.PoolTimeout,  # 连接池耗尽
        ) as e:
            # 网络/协议错误 — 指数退避重试
            last_error = e
            if attempt + 1 >= attempt_count:
                break
            wait = min(2**attempt, 8)
            logger.warning(
                f"[{model_id}] 网络错误 ({type(e).__name__}): {e}，等待 {wait}s 后重试 (第 {attempt + 1}/{attempt_count} 次)"
            )
            time.sleep(wait)

    # 所有重试都失败
    raise last_error


# ══════════════════════════════════════════════════════════════
# Provider: OpenAI-compatible (DeepSeek / Ark / OpenAI / ...)
# ══════════════════════════════════════════════════════════════


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
                endpoint,
                msgs,
                current_max_tokens,
                temperature,
                timeout,
                reasoning_effort=reasoning_effort,
            )
        else:
            content, finish = _call_openai_compat_once(
                endpoint,
                msgs,
                current_max_tokens,
                temperature,
                timeout,
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
                expanded = max(
                    current_max_tokens * 2,
                    current_max_tokens + 256,
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
            # There is no visible fragment that can be continued safely.
            # Fall through to the bounded fail-closed path below.
            attempts = MAX_CONTINUATIONS
        # 被截断 → 追加部分内容为 assistant，再请求"继续"
        if attempts >= MAX_CONTINUATIONS:
            repaired = _finalize_structured_text(combined)
            if repaired != combined and _structured_response_kind(combined) == "json":
                logger.warning("结构化 JSON 在续写次数耗尽后已修复，避免返回非法协议")
                return _mark_truncated(repaired)
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
        msgs.append(
            {
                "role": "user",
                "content": _continuation_prompt(kind),
            }
        )


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
    if any(
        marker in lowered
        for marker in (
            "<||dsml||tool_calls",
            "<uses_legacy_tools",
            "<tool_calls",
        )
    ):
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
    """单次 OpenAI 兼容调用，返回 (content, finish_reason)。

    网络错误时会抛出 PartialResponseError，携带已接收的部分内容（如果有）。
    """
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

    # Phase 1: 捕获网络错误时的部分响应
    partial_content = ""
    partial_usage = None
    model_id = f"{endpoint.provider}/{endpoint.model_name}"

    try:
        resp = client.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        # §8.8.1：提取并累加真实 usage（不再丢弃），+ model_id 用于缓存追踪
        _acc_usage(endpoint.provider, data, model_id)
        msg = data["choices"][0]["message"]
        finish = data["choices"][0].get("finish_reason", "")
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or msg.get("thinking") or ""

        if content:
            logger.debug(f"API 响应: content={content[:300]}")
        elif reasoning:
            logger.debug(f"API 响应: content=空, reasoning_content={reasoning[:300]}")
        else:
            logger.warning(
                f"API 响应: content 和 reasoning_content 均为空! finish_reason={finish}"
            )

        # 推理模型在正常结束时偶尔只返回 reasoning_content；保留兼容兜底。
        # ``length`` 表示该推理本身尚未完成，绝不能把它冒充最终答案或协议正文。
        if not content and reasoning and finish != "length":
            content = reasoning

        return content, finish

    except (
        httpx.ReadTimeout,
        httpx.ConnectTimeout,
        httpx.RemoteProtocolError,
        httpx.WriteError,
        httpx.PoolTimeout,
    ) as e:
        # 网络错误：尝试从部分响应中提取内容
        # 注意：同步请求在网络错误时通常无法获取部分响应体
        # 这里我们记录错误类型，为后续流式实现做准备
        if partial_content:
            partial = PartialContent(
                content=partial_content,
                tokens_generated=0,  # 同步请求无法准确估算
                model_id=model_id,
                finish_reason=type(e).__name__,
                usage=partial_usage,
            )
            raise PartialResponseError(partial, e) from e
        # 没有部分内容，直接抛出原始异常
        raise


# ═══════════════════════════════════════════════════════
# Provider: Anthropic-native
# ═══════════════════════════════════════════════════════


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
            endpoint, msgs, max_tokens, temperature, timeout
        )
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
    """单次 Anthropic 调用，返回 (text, stop_reason)。

    网络错误时会抛出 PartialResponseError，携带已接收的部分内容（如果有）。
    """
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

    # Phase 1: 捕获网络错误时的部分响应
    model_id = f"{endpoint.provider}/{endpoint.model_name}"

    try:
        resp = client.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        # §8.8.1：提取并累加真实 usage（Anthropic 用 input/output_tokens）
        _acc_usage(endpoint.provider, data, model_id)
        # content 是文本块列表；拼接所有 text 块（比仅取 [0] 更鲁棒）
        blocks = data.get("content", []) or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        stop_reason = data.get("stop_reason", "")
        return text, stop_reason

    except (
        httpx.ReadTimeout,
        httpx.ConnectTimeout,
        httpx.RemoteProtocolError,
        httpx.WriteError,
        httpx.PoolTimeout,
    ):
        # 网络错误：尝试从部分响应中提取内容
        # 同步请求在网络错误时通常无法获取部分响应体
        # 这里为后续流式实现预留接口
        raise  # 暂时直接抛出，等 Phase 1.3 实现流式捕获


# ── R3: 原生 function-calling 能力（Q2 三层降级前置） ──────


def _normalize_openai_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """OpenAI 兼容厂商直接透传 tools（已是 {type:function, function:{...}} 形态）。"""
    if not tools:
        return None
    return [t if t.get("type") else {"type": "function", "function": t} for t in tools]


def _openai_to_anthropic_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """把 OpenAI 风格 tools 转为 Anthropic 原生格式 [{name, description, input_schema}]。"""
    if not tools:
        return None
    converted = []
    for t in tools:
        fn = t.get("function", t)  # 兼容裸函数定义
        converted.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters")
                or fn.get("input_schema")
                or {"type": "object", "properties": {}},
            }
        )
    return converted


def _parse_openai_tool_calls(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 OpenAI message.tool_calls 为统一结构。"""
    out = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        args_raw = fn.get("arguments", "{}")
        try:
            args = (
                json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            )
        except (json.JSONDecodeError, TypeError):
            # 参数非合法 JSON — 保留原始字符串，调用方自行处理
            args = {"_raw": args_raw}
        out.append(
            {
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "arguments": args,
            }
        )
    return out


def _parse_anthropic_tool_calls(
    blocks: list[Any],
) -> tuple[str, list[dict[str, Any]], str]:
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
            tool_calls.append(
                {
                    "id": b.get("id", ""),
                    "name": b.get("name", ""),
                    "arguments": b.get("input") or {},
                }
            )
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
            system_parts.append(
                content
                if isinstance(content, str)
                else json.dumps(content, ensure_ascii=False)
            )
            continue
        if role == "tool":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": str(message.get("tool_call_id", "")),
                    "content": content
                    if isinstance(content, str)
                    else json.dumps(content, ensure_ascii=False),
                }
            )
            continue

        flush_tool_results()
        if role == "assistant" and message.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if content:
                blocks.append({"type": "text", "text": str(content)})
            for tool_call in message.get("tool_calls", []):
                function = (
                    tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
                )
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {"_raw": arguments}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(tool_call.get("id", "")),
                        "name": str(function.get("name", "")),
                        "input": arguments if isinstance(arguments, dict) else {},
                    }
                )
            chat_messages.append({"role": "assistant", "content": blocks})
            continue

        chat_messages.append({"role": role, "content": content})

    flush_tool_results()
    return "\n\n".join(part for part in system_parts if part), chat_messages


def chat_completion_with_tools(
    model_id: str,
    messages: list[dict[str, str]],
    *,
    tools: list[dict[str, Any]] | None = None,
    response_format: dict[str, Any] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    credentials: dict[str, str] | None = None,
    base_url: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    reasoning_effort: str | None = None,
    cache_context: dict[str, Any] | None = None,
    cache_lane_registry: Any = None,
    timeout: float = 120.0,
    max_retries: int = 3,
) -> LLMResponse:
    """带原生 function-calling 的 chat completion（R3 / Q2 三层降级前置）。

    - OpenAI 兼容厂商：tools/response_format/tool_choice 直接透传；
    - Anthropic：tools 转原生格式，response_format 以 system 提示词降级（Anthropic
      无 OpenAI 风格 JSON mode，靠提示词 + 解析兜底），tool_choice 映射到
      anthropic 的 tool_choice（auto/any/tool）；
    - 返回 LLMResponse（content + tool_calls + finish_reason），不抛业务异常
      之外的错误（429/5xx/网络仍走重试，与 chat_completion 一致）。

    无 tools/response_format 时，行为退化为普通文本调用，但仍返回 LLMResponse
    结构（F5 三层降级可据此统一处理）。
    """
    import time

    endpoint = build_endpoint(model_id, credentials, base_url)
    compiled = compile_prompt(messages, tools=tools)
    messages = compiled.messages
    tools = compiled.tools
    response_format = canonicalize_request_value(response_format)
    tool_choice = canonicalize_request_value(tool_choice)
    request_shape = {
        "response_format": response_format,
        "tool_choice": tool_choice,
    }
    cache_context = _prepare_cache_lane(
        cache_lane_registry,
        model_id,
        messages,
        tools=tools,
        request_shape=request_shape,
        cache_context=cache_context,
    )
    _set_cache_manifest(
        model_id,
        messages,
        tools=tools,
        request_shape=request_shape,
        cache_context=cache_context,
        prompt_layout=compiled.layout(),
    )
    reasoning_effort = _normalize_reasoning_effort(reasoning_effort)
    last_error: Exception | None = None

    # Keep the callback contract identical to ``chat_completion``.  Native
    # function-calling used to return usage on LLMResponse only, which made
    # UsageTracker and Cache Rails report zero calls for tool-heavy engines.
    _usage_tl.usage_acc = LLMUsage()
    started_at = time.monotonic()

    attempt_count = max(1, max_retries)
    for attempt in range(attempt_count):
        try:
            if endpoint.provider == "anthropic":
                response = _call_anthropic_with_tools(
                    endpoint,
                    messages,
                    tools,
                    response_format,
                    tool_choice,
                    max_tokens,
                    temperature,
                    timeout,
                )
            elif reasoning_effort:
                response = _call_openai_compat_with_tools(
                    endpoint,
                    messages,
                    tools,
                    response_format,
                    tool_choice,
                    max_tokens,
                    temperature,
                    timeout,
                    reasoning_effort=reasoning_effort,
                )
            else:
                response = _call_openai_compat_with_tools(
                    endpoint,
                    messages,
                    tools,
                    response_format,
                    tool_choice,
                    max_tokens,
                    temperature,
                    timeout,
                )
            accumulated = getattr(_usage_tl, "usage_acc", None)
            usage = accumulated if isinstance(accumulated, LLMUsage) else response.usage
            _emit_usage(model_id, usage, time.monotonic() - started_at)
            return response
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 429 or 500 <= status < 600:
                last_error = e
                if attempt + 1 >= attempt_count:
                    break
                wait = _retry_delay(e, attempt)
                logger.warning(
                    f"[{model_id}] {status} 失败，等待 {wait:g}s 重试 (第 {attempt + 1}/{attempt_count} 次)"
                )
                time.sleep(wait)
            else:
                raise
        except (
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.RemoteProtocolError,
            httpx.WriteError,
            httpx.PoolTimeout,
        ) as e:
            last_error = e
            if attempt + 1 >= attempt_count:
                break
            wait = min(2**attempt, 8)
            logger.warning(
                f"[{model_id}] 网络错误 ({type(e).__name__}): {e}，等待 {wait}s 重试"
            )
            time.sleep(wait)

    raise last_error  # type: ignore[misc]


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
    if not data or not isinstance(data, dict):
        raise RuntimeError(f"空响应或非 JSON: {resp.text[:200]}")
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
    content = _normalize_structured_final_content(content, tool_calls, response_format)
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
        system_text = (
            system_text + "\n\n" if system_text else ""
        ) + "请严格以合法 JSON 输出，不要包含多余文本。"

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
            payload["tool_choice"] = {
                "type": "tool",
                "name": tool_choice.get("function", {}).get("name", ""),
            }
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
    finish = (
        "tool_calls"
        if stop == "tool_use"
        else ("length" if stop == "max_tokens" else stop or "stop")
    )
    if finish == "length":
        raise ResponseTruncatedError(
            "Anthropic 原生工具调用响应因 stop_reason=max_tokens 被截断，"
            "为避免执行不完整的工具参数，已拒绝该响应。"
        )
    text = _normalize_structured_final_content(text, tool_calls, response_format)
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


def _normalize_structured_final_content(
    content: str,
    tool_calls: list[dict[str, Any]],
    response_format: dict[str, Any] | None,
) -> str:
    """Keep one complete JSON result; reject a cut one before tolerant repair."""
    if (
        not response_format
        or not content
        or tool_calls
        or "json" not in json.dumps(response_format).lower()
    ):
        return content
    stripped = content.lstrip()
    try:
        parsed, end = json.JSONDecoder(strict=False).raw_decode(stripped)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        # parse_react can repair a cut JSON string by adding closing quotes and
        # braces.  That is useful for recovering tool envelopes, but a repaired
        # final_answer is still semantically incomplete.  Reject it here so the
        # engine retries instead of presenting a fragment.
        raise ResponseTruncatedError(
            "原生结构化响应不是完整 JSON，拒绝把修复后的残缺回答当作最终结果"
        ) from exc
    if not isinstance(parsed, (dict, list)):
        raise ResponseTruncatedError("原生结构化响应未返回 JSON 对象或数组")
    if stripped[end:].strip():
        logger.warning("原生结构化响应在完整 JSON 后含额外文本，已丢弃额外部分")
        return json.dumps(parsed, ensure_ascii=False)
    return content


# ── 流式调用接口 ──────────────────────────────────────────


def chat_completion_stream(
    model_id: str,
    messages: list[dict[str, str]],
    *,
    credentials: dict[str, str] | None = None,
    base_url: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    reasoning_effort: str | None = None,
    cache_context: dict[str, Any] | None = None,
    cache_lane_registry: Any = None,
    timeout: float = 300.0,
    checkpoint_manager: Any | None = None,
) -> Generator[str, None, None]:
    """
    流式 chat completion 调用。

    Args:
        checkpoint_manager: 可选的 CheckpointManager 实例，用于周期性保存检查点

    Yields:
        逐步生成的文本片段（delta）。
    """
    endpoint = build_endpoint(model_id, credentials, base_url)
    compiled = compile_prompt(messages)
    messages = compiled.messages
    cache_context = _prepare_cache_lane(
        cache_lane_registry,
        model_id,
        messages,
        cache_context=cache_context,
    )
    manifest = _set_cache_manifest(
        model_id,
        messages,
        cache_context=cache_context,
        prompt_layout=compiled.layout(),
    )
    reasoning_effort = _normalize_reasoning_effort(reasoning_effort)

    if endpoint.provider == "anthropic":
        yield from _stream_anthropic(
            endpoint,
            messages,
            max_tokens,
            temperature,
            timeout,
            model_id,
            manifest,
            checkpoint_manager=checkpoint_manager,
        )
    else:
        yield from _stream_openai_compat(
            endpoint,
            messages,
            max_tokens,
            temperature,
            timeout,
            model_id,
            reasoning_effort=reasoning_effort,
            manifest=manifest,
            checkpoint_manager=checkpoint_manager,
        )


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
    checkpoint_manager: Any | None = None,
) -> Generator[str, None, None]:
    """OpenAI 兼容格式流式调用。

    P3-Q1 续 / §8.8.1：机会性提取末尾 chunk 的 ``usage``（部分兼容厂商默认随
    末帧返回；OpenAI 官方需 ``stream_options.include_usage``，此处不强加以避免
    对不支持的厂商触发 400）。提取到则经 usage 回调发出真实 token 用量。

    Phase 3: 支持检查点管理 - 周期性保存生成进度，网络中断时可从检查点恢复。
    """
    import time
    from xenon.utils.token_estimator import estimate_tokens

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
    finish_reason = ""

    # Phase 3: 检查点管理初始化
    accumulated_content = ""
    accumulated_tokens = 0

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
                choice = choices[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta", {})
                content = delta.get("content")
                if content:
                    # Phase 3: 累积内容和 token 数
                    accumulated_content += content
                    accumulated_tokens += estimate_tokens(content)

                    # Phase 3: 检查是否需要保存检查点
                    if checkpoint_manager and checkpoint_manager.should_save(accumulated_tokens):
                        checkpoint = checkpoint_manager.save_checkpoint(
                            accumulated_content,
                            accumulated_tokens,
                            metadata={"model_id": model_id, "provider": endpoint.provider}
                        )
                        logger.debug(
                            f"流式生成检查点 #{checkpoint.sequence} "
                            f"(tokens={accumulated_tokens}, age={checkpoint.age():.1f}s)"
                        )

                    yield content
    if usage_data is not None:
        _emit_usage(
            model_id, _extract_usage(usage_data, endpoint.provider), time.time() - t0
        )
        # 发出响应回调（供 CacheTracker 等订阅原始 API 响应）
        _emit_response(model_id, _response_with_manifest(usage_data, manifest))
    if finish_reason == "length":
        raise ResponseTruncatedError(
            "流式 API 响应因 finish_reason=length 被截断，已拒绝把残缺内容当作完整回答"
        )


def _stream_anthropic(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
    model_id: str,
    manifest: dict[str, Any] | None = None,
    checkpoint_manager: Any | None = None,
) -> Generator[str, None, None]:
    """Anthropic 原生格式流式调用。

    P3-Q1 续 / §8.8.1：从 ``message_start`` 取 input_tokens、``message_delta``
    取 output_tokens（末值为最终输出），结束后经 usage 回调发出真实用量。

    Phase 3: 支持检查点管理 - 周期性保存生成进度，网络中断时可从检查点恢复。
    """
    import time
    from xenon.utils.token_estimator import estimate_tokens

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
    stop_reason = ""

    # Phase 3: 检查点管理初始化
    accumulated_content = ""
    accumulated_tokens = 0

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
                    stop_reason = (event.get("delta") or {}).get(
                        "stop_reason"
                    ) or stop_reason
                    u = event.get("usage") or {}
                    if "output_tokens" in u:
                        output_tokens = int(u.get("output_tokens", 0) or 0)
                elif etype == "content_block_delta":
                    delta = event.get("delta", {})
                    text = delta.get("text")
                    if text:
                        # Phase 3: 累积内容和 token 数
                        accumulated_content += text
                        accumulated_tokens += estimate_tokens(text)

                        # Phase 3: 检查是否需要保存检查点
                        if checkpoint_manager and checkpoint_manager.should_save(accumulated_tokens):
                            checkpoint = checkpoint_manager.save_checkpoint(
                                accumulated_content,
                                accumulated_tokens,
                                metadata={"model_id": model_id, "provider": "anthropic"}
                            )
                            logger.debug(
                                f"流式生成检查点 #{checkpoint.sequence} "
                                f"(tokens={accumulated_tokens}, age={checkpoint.age():.1f}s)"
                            )

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
                {
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    }
                },
                manifest,
            ),
        )
    if stop_reason == "max_tokens":
        raise ResponseTruncatedError(
            "Anthropic 流式响应因 stop_reason=max_tokens 被截断，"
            "已拒绝把残缺内容当作完整回答"
        )
