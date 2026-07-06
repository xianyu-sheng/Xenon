"""
LLM Client — 多厂商统一调用适配器。

职责：
1. 从全局凭证文件 (~/.omniagent/credentials.yaml) 加载 API Key。
2. 根据 model_id 前缀 (如 "anthropic/claude-3-5-sonnet") 路由到对应厂商的 HTTP 端点。
3. 封装统一的 chat completion 调用，返回纯文本。
"""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import logging
import httpx
import yaml

logger = logging.getLogger(__name__)


# B12: finish_reason=length（OpenAI 兼容）/ stop_reason=max_tokens（Anthropic）
# 时自动续写的最大次数；耗尽后抛 ResponseTruncatedError，而不是仅 logger.warning
# 后静默返回被截断的内容。
MAX_CONTINUATIONS = 3


class ResponseTruncatedError(RuntimeError):
    """LLM 响应因 max_tokens 上限被截断，且续写次数耗尽仍不完整。"""


# ── 安全代理处理 ────────────────────────────────────────────

def _build_proxy_config() -> httpx.Proxy | None:
    """
    从环境变量构建 httpx 兼容的代理配置。

    httpx 不支持 socks:// 代理，而部分用户环境可能设置了
    ALL_PROXY=socks://...（如 Clash 的混合端口），直接传给 httpx 会抛
    ValueError: Unknown scheme for proxy URL。

    此函数优先使用 HTTPS_PROXY/HTTP_PROXY，忽略不支持的 socks://。
    """
    for env_name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.getenv(env_name)
        if val and (val.startswith("http://") or val.startswith("https://")):
            return httpx.Proxy(url=val)

    # ALL_PROXY 只在使用 http/https 协议时才接受
    for env_name in ("ALL_PROXY", "all_proxy"):
        val = os.getenv(env_name)
        if val and (val.startswith("http://") or val.startswith("https://")):
            return httpx.Proxy(url=val)

    return None


def _create_http_client(
    timeout: float = 120.0,
    proxy: httpx.Proxy | None | object = _build_proxy_config,  # sentinel
    **kwargs: Any,
) -> httpx.Client:
    """
    创建带安全代理配置的 httpx.Client。

    自动从环境变量读取代理设置，过滤掉 httpx 不支持的 socks:// 协议。
    可通过 proxy=None 强制不走代理。
    额外关键字参数透传给 httpx.Client（如 follow_redirects）。
    """
    if proxy is _build_proxy_config:
        proxy = _build_proxy_config()
    return httpx.Client(timeout=timeout, proxy=proxy, **kwargs)


# ── 全局凭证路径 ──────────────────────────────────────────
_CREDENTIALS_PATH = Path.home() / ".omniagent" / "credentials.yaml"


@dataclass
class ModelEndpoint:
    """单个模型的调用元信息。"""

    provider: str        # "openai" | "anthropic" | "deepseek"
    model_name: str      # 厂商侧模型名，如 "claude-3-5-sonnet-20241022"
    base_url: str        # API 基础地址
    api_key: str = field(repr=False, default="")
    max_tokens: int = 4096


# ── 厂商默认配置 ──────────────────────────────────────────
_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "max_output_tokens": 16384,
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "env_key": "ANTHROPIC_API_KEY",
        "max_output_tokens": 8192,
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "env_key": "DEEPSEEK_API_KEY",
        "max_output_tokens": 8192,
    },
    "google": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env_key": "GOOGLE_API_KEY",
        "max_output_tokens": 8192,
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "env_key": "ZHIPU_API_KEY",
        "max_output_tokens": 8192,
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env_key": "QWEN_API_KEY",
        "max_output_tokens": 8192,
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "env_key": "MOONSHOT_API_KEY",
        "max_output_tokens": 8192,
    },
    "baichuan": {
        "base_url": "https://api.baichuan-ai.com/v1",
        "env_key": "BAICHUAN_API_KEY",
        "max_output_tokens": 4096,
    },
    "minimax": {
        "base_url": "https://api.minimax.chat/v1",
        "env_key": "MINIMAX_API_KEY",
        "max_output_tokens": 4096,
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "env_key": "OLLAMA_API_KEY",
        "max_output_tokens": 32768,
    },
    "xiaomi": {
        "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
        "env_key": "XIAOMI_API_KEY",
        "max_output_tokens": 8192,
    },
}


def _load_credentials() -> dict[str, str]:
    """从 ~/.omniagent/credentials.yaml 或环境变量加载 API Key。"""
    creds: dict[str, str] = {}
    if _CREDENTIALS_PATH.exists():
        with open(_CREDENTIALS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            creds = {k.lower(): v for k, v in data.items()}

    # 环境变量作为补充 / 覆盖
    for provider, cfg in _PROVIDER_DEFAULTS.items():
        env_val = os.getenv(cfg["env_key"])
        if env_val:
            creds[provider] = env_val
    return creds


def parse_model_id(model_id: str) -> tuple[str, str]:
    """
    解析 'provider/model_name' 格式的 model_id。
    例: "anthropic/claude-3-5-sonnet" -> ("anthropic", "claude-3-5-sonnet")
    """
    if "/" not in model_id:
        raise ValueError(
            f"model_id 必须为 'provider/model_name' 格式，收到: {model_id}"
        )
    provider, name = model_id.split("/", 1)
    return provider.lower(), name


def build_endpoint(model_id: str, credentials: dict[str, str] | None = None, base_url: str | None = None) -> ModelEndpoint:
    """根据 model_id 构建完整的调用端点信息。"""
    provider, model_name = parse_model_id(model_id)
    if provider not in _PROVIDER_DEFAULTS:
        raise ValueError(f"不支持的 provider: {provider}，支持: {list(_PROVIDER_DEFAULTS.keys())}")

    defaults = _PROVIDER_DEFAULTS[provider]
    creds = credentials or _load_credentials()
    api_key = creds.get(provider, "")
    if not api_key:
        raise ValueError(
            f"未找到 {provider} 的 API Key。"
            f"请在 {_CREDENTIALS_PATH} 或环境变量 {defaults['env_key']} 中配置。"
        )
    return ModelEndpoint(
        provider=provider,
        model_name=model_name,
        base_url=base_url or defaults["base_url"],
        api_key=api_key,
    )


# ── 统一调用接口 ──────────────────────────────────────────


def chat_completion(
    model_id: str,
    messages: list[dict[str, str]],
    *,
    credentials: dict[str, str] | None = None,
    base_url: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
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
    # B4: 按厂商输出上限钳制 max_tokens，防止 131072 等超限值引发 400 级联失败
    provider_cap = _PROVIDER_DEFAULTS.get(endpoint.provider, {}).get("max_output_tokens")
    if provider_cap and max_tokens > provider_cap:
        max_tokens = provider_cap
    last_error = None

    for attempt in range(max_retries):
        try:
            if endpoint.provider == "anthropic":
                return _call_anthropic(endpoint, messages, max_tokens, temperature, timeout)
            else:
                return _call_openai_compat(endpoint, messages, max_tokens, temperature, timeout)

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 429:
                # 限流 — 指数退避
                wait = 2 ** attempt
                logger.warning(f"[{model_id}] 429 限流，等待 {wait}s 后重试 (第 {attempt + 1}/{max_retries} 次)")
                time.sleep(wait)
                last_error = e
            elif 500 <= status < 600:
                # 服务端错误 — 指数退避重试
                wait = 2 ** attempt
                logger.warning(f"[{model_id}] {status} 服务端错误，等待 {wait}s 后重试 (第 {attempt + 1}/{max_retries} 次)")
                time.sleep(wait)
                last_error = e
            else:
                # 其他 HTTP 错误 — 不重试
                raise

        except (
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.RemoteProtocolError,   # "Server disconnected without sending a response"
            httpx.WriteError,            # 写入连接失败
            httpx.PoolTimeout,           # 连接池耗尽
        ) as e:
            # 网络/协议错误 — 指数退避重试
            wait = min(2 ** attempt, 8)
            logger.warning(f"[{model_id}] 网络错误 ({type(e).__name__}): {e}，等待 {wait}s 后重试 (第 {attempt + 1}/{max_retries} 次)")
            time.sleep(wait)
            last_error = e

    # 所有重试都失败
    raise last_error


def _call_openai_compat(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> str:
    """OpenAI 兼容格式调用（B12: finish_reason=length 自动续写）。"""
    msgs = list(messages)  # 不修改调用方列表
    parts: list[str] = []
    attempts = 0
    while True:
        content, finish = _call_openai_compat_once(
            endpoint, msgs, max_tokens, temperature, timeout)
        if content:
            parts.append(content)
        if finish != "length":
            return "".join(parts)
        # 被截断 → 追加部分内容为 assistant，再请求"继续"
        if attempts >= MAX_CONTINUATIONS:
            raise ResponseTruncatedError(
                f"API 响应在 {MAX_CONTINUATIONS} 次续写后仍被截断 "
                f"(finish_reason=length)，内容可能不完整；请增大 max_tokens 或精简输入。"
            )
        attempts += 1
        logger.info("API 响应被截断 (finish_reason=length)，自动续写…")
        msgs.append({"role": "assistant", "content": content or ""})
        msgs.append({"role": "user", "content": "继续"})


def _call_openai_compat_once(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
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
    with _create_http_client(timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
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

        # 推理模型：content 可能为空，真正的答案在 reasoning_content 末尾
        if not content and reasoning:
            content = reasoning

        return content, finish


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
    # Anthropic 要求 system 单独传递
    system_text = ""
    chat_messages = []
    for msg in messages:
        if msg["role"] == "system":
            system_text = msg["content"]
        else:
            chat_messages.append(msg)

    payload: dict[str, Any] = {
        "model": endpoint.model_name,
        "messages": chat_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system_text:
        payload["system"] = system_text

    with _create_http_client(timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        # content 是文本块列表；拼接所有 text 块（比仅取 [0] 更鲁棒）
        blocks = data.get("content", []) or []
        text = "".join(
            b.get("text", "") for b in blocks if isinstance(b, dict)
        )
        stop_reason = data.get("stop_reason", "")
        return text, stop_reason


# ── 流式调用接口 ──────────────────────────────────────────


def chat_completion_stream(
    model_id: str,
    messages: list[dict[str, str]],
    *,
    credentials: dict[str, str] | None = None,
    base_url: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    timeout: float = 300.0,
) -> Generator[str, None, None]:
    """
    流式 chat completion 调用。

    Yields:
        逐步生成的文本片段（delta）。
    """
    endpoint = build_endpoint(model_id, credentials, base_url)

    if endpoint.provider == "anthropic":
        yield from _stream_anthropic(endpoint, messages, max_tokens, temperature, timeout)
    else:
        yield from _stream_openai_compat(endpoint, messages, max_tokens, temperature, timeout)


def _stream_openai_compat(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> Generator[str, None, None]:
    """OpenAI 兼容格式流式调用。"""
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
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue


def _stream_anthropic(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> Generator[str, None, None]:
    """Anthropic 原生格式流式调用。"""
    url = f"{endpoint.base_url}/v1/messages"
    headers = {
        "x-api-key": endpoint.api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    system_text = ""
    chat_messages = []
    for msg in messages:
        if msg["role"] == "system":
            system_text = msg["content"]
        else:
            chat_messages.append(msg)

    payload: dict[str, Any] = {
        "model": endpoint.model_name,
        "messages": chat_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    if system_text:
        payload["system"] = system_text

    with _create_http_client(timeout=timeout) as client:
        with client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                try:
                    event = json.loads(data_str)
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        text = delta.get("text")
                        if text:
                            yield text
                except (json.JSONDecodeError, KeyError):
                    continue
