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
from urllib.parse import urlparse

import logging
import httpx
import yaml

logger = logging.getLogger(__name__)

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
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "env_key": "ANTHROPIC_API_KEY",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "env_key": "DEEPSEEK_API_KEY",
    },
    "google": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env_key": "GOOGLE_API_KEY",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "env_key": "ZHIPU_API_KEY",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env_key": "QWEN_API_KEY",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "env_key": "MOONSHOT_API_KEY",
    },
    "baichuan": {
        "base_url": "https://api.baichuan-ai.com/v1",
        "env_key": "BAICHUAN_API_KEY",
    },
    "minimax": {
        "base_url": "https://api.minimax.chat/v1",
        "env_key": "MINIMAX_API_KEY",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "env_key": "OLLAMA_API_KEY",
    },
    "xiaomi": {
        "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
        "env_key": "XIAOMI_API_KEY",
    },
}


def _normalize_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/") if isinstance(base_url, str) else ""


def _openai_compat_url(endpoint: ModelEndpoint, path: str) -> str:
    """Build an OpenAI-compatible URL, adding /v1 for root OpenAI relays."""

    base_url = endpoint.base_url.rstrip("/")
    parsed = urlparse(base_url)
    if endpoint.provider == "openai" and parsed.scheme and parsed.netloc and parsed.path.rstrip("/") in ("", "/"):
        base_url = f"{base_url}/v1"
    return f"{base_url}/{path.lstrip('/')}"


def _credential_api_key(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("api_key", "key", "token"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
        return ""
    return value.strip() if isinstance(value, str) else ""


def _credential_base_url(value: Any) -> str:
    if isinstance(value, dict):
        item = value.get("base_url")
        if isinstance(item, str):
            return _normalize_base_url(item)
    return ""


def _load_credentials() -> dict[str, Any]:
    """从 ~/.omniagent/credentials.yaml 加载 API Key，环境变量可覆盖。

    委托 provider_registry.load_credentials() 读取文件（单一解析路径），
    在此之上附加环境变量覆盖。消除旧版双重解析。
    """
    # 从 provider_registry 统一加载文件凭证（避免重复解析）
    creds: dict[str, Any] = {}
    try:
        from omniagent.repl.provider_registry import load_credentials as _load_from_file
        creds = {k.lower(): v for k, v in _load_from_file().items()}
    except Exception:
        # 回退：直接读取文件
        if _CREDENTIALS_PATH.exists():
            with open(_CREDENTIALS_PATH, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                creds = {k.lower(): v for k, v in data.items()}

    # 环境变量作为补充 / 覆盖
    for provider, cfg in _PROVIDER_DEFAULTS.items():
        env_val = os.getenv(cfg["env_key"])
        if env_val:
            existing = creds.get(provider)
            if isinstance(existing, dict):
                updated = dict(existing)
                updated["api_key"] = env_val
                creds[provider] = updated
            else:
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


def build_endpoint(model_id: str, credentials: dict[str, Any] | None = None) -> ModelEndpoint:
    """根据 model_id 构建完整的调用端点信息。"""
    provider, model_name = parse_model_id(model_id)
    if provider not in _PROVIDER_DEFAULTS:
        raise ValueError(f"不支持的 provider: {provider}，支持: {list(_PROVIDER_DEFAULTS.keys())}")

    defaults = _PROVIDER_DEFAULTS[provider]
    creds = credentials or _load_credentials()
    credential = creds.get(provider, "")
    api_key = _credential_api_key(credential)
    if not api_key:
        raise ValueError(
            f"未找到 {provider} 的 API Key。"
            f"请在 {_CREDENTIALS_PATH} 或环境变量 {defaults['env_key']} 中配置。"
        )
    base_url = _credential_base_url(credential) or defaults["base_url"]
    return ModelEndpoint(
        provider=provider,
        model_name=model_name,
        base_url=base_url,
        api_key=api_key,
    )


# ── 统一调用接口 ──────────────────────────────────────────


def chat_completion(
    model_id: str,
    messages: list[dict[str, str]],
    *,
    credentials: dict[str, Any] | None = None,
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

    endpoint = build_endpoint(model_id, credentials)
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
    """OpenAI 兼容格式调用。"""
    url = _openai_compat_url(endpoint, "chat/completions")
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
    with httpx.Client(timeout=timeout) as client:
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

        # 如果被截断，提示
        if finish == "length":
            logger.warning(f"API 响应被截断 (finish_reason=length)，考虑增大 max_tokens")

        return content


def _call_anthropic(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> str:
    """Anthropic 原生 API 格式调用。"""
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

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]


# ── 流式调用接口 ──────────────────────────────────────────


def chat_completion_stream(
    model_id: str,
    messages: list[dict[str, str]],
    *,
    credentials: dict[str, Any] | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    timeout: float = 300.0,
) -> Generator[str, None, None]:
    """
    流式 chat completion 调用。

    Yields:
        逐步生成的文本片段（delta）。
    """
    endpoint = build_endpoint(model_id, credentials)

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
    url = _openai_compat_url(endpoint, "chat/completions")
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
    has_content = False
    reasoning_parts: list[str] = []
    with httpx.Client(timeout=timeout) as client:
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
                    reasoning = delta.get("reasoning_content") or delta.get("thinking") or ""
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    if content:
                        has_content = True
                        yield content
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

    # ── 推理模型回退：流式 delta 只含 reasoning_content 而 content 为空 ──
    # 阻塞式调用已处理此情况（line 288-290），流式此前缺失此逻辑，
    # 导致 LLM 消耗了 token 但用户看到空白输出。
    if not has_content and reasoning_parts:
        reasoning_text = "".join(reasoning_parts)
        logger.warning(
            f"流式响应: content 为空，回退到 reasoning_content "
            f"({len(reasoning_text)} 字符)"
        )
        yield reasoning_text


# ── 异步调用接口 ──────────────────────────────────────────

async def chat_completion_async(
    model_id: str,
    messages: list[dict[str, str]],
    *,
    credentials: dict[str, Any] | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    timeout: float = 120.0,
    max_retries: int = 3,
) -> str:
    """异步 chat completion 调用（带重试）。

    与 chat_completion 功能相同，但使用 httpx.AsyncClient 实现非阻塞调用。
    """
    import asyncio

    endpoint = build_endpoint(model_id, credentials)
    last_error = None

    for attempt in range(max_retries):
        try:
            if endpoint.provider == "anthropic":
                return await _call_anthropic_async(endpoint, messages, max_tokens, temperature, timeout)
            else:
                return await _call_openai_compat_async(endpoint, messages, max_tokens, temperature, timeout)

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 429:
                wait = 2 ** attempt
                logger.warning(f"[{model_id}] 429 限流，等待 {wait}s 后重试 (第 {attempt + 1}/{max_retries} 次)")
                await asyncio.sleep(wait)
                last_error = e
            elif 500 <= status < 600:
                wait = 2 ** attempt
                logger.warning(f"[{model_id}] {status} 服务端错误，等待 {wait}s 后重试 (第 {attempt + 1}/{max_retries} 次)")
                await asyncio.sleep(wait)
                last_error = e
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
            wait = min(2 ** attempt, 8)
            logger.warning(f"[{model_id}] 网络错误 ({type(e).__name__}): {e}，等待 {wait}s 后重试 (第 {attempt + 1}/{max_retries} 次)")
            await asyncio.sleep(wait)
            last_error = e

    raise last_error


async def _call_openai_compat_async(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> str:
    """OpenAI 兼容格式异步调用。"""
    url = _openai_compat_url(endpoint, "chat/completions")
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
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
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

        if not content and reasoning:
            content = reasoning

        if finish == "length":
            logger.warning(f"API 响应被截断 (finish_reason=length)，考虑增大 max_tokens")

        return content


async def _call_anthropic_async(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> str:
    """Anthropic 原生 API 格式异步调用。"""
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
    }
    if system_text:
        payload["system"] = system_text

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]


async def chat_completion_stream_async(
    model_id: str,
    messages: list[dict[str, str]],
    *,
    credentials: dict[str, Any] | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    timeout: float = 300.0,
):
    """异步流式 chat completion。返回 async generator。"""
    endpoint = build_endpoint(model_id, credentials)

    if endpoint.provider == "anthropic":
        async for chunk in _stream_anthropic_async(endpoint, messages, max_tokens, temperature, timeout):
            yield chunk
    else:
        async for chunk in _stream_openai_compat_async(endpoint, messages, max_tokens, temperature, timeout):
            yield chunk


async def _stream_openai_compat_async(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
):
    """OpenAI 兼容格式异步流式调用。"""
    url = _openai_compat_url(endpoint, "chat/completions")
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
    has_content = False
    reasoning_parts: list[str] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content")
                    reasoning = delta.get("reasoning_content") or delta.get("thinking") or ""
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    if content:
                        has_content = True
                        yield content
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

    # ── 推理模型回退：流式 delta 只含 reasoning_content 而 content 为空 ──
    if not has_content and reasoning_parts:
        reasoning_text = "".join(reasoning_parts)
        logger.warning(
            f"异步流式响应: content 为空，回退到 reasoning_content "
            f"({len(reasoning_text)} 字符)"
        )
        yield reasoning_text


async def _stream_anthropic_async(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
):
    """Anthropic 原生格式异步流式调用。"""
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

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
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

    with httpx.Client(timeout=timeout) as client:
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


# ═══════════════════════════════════════════════════════════════
# Native Tool Calling — 原生函数调用 API, 消除 JSON 解析根因
# ═══════════════════════════════════════════════════════════════


class NativeToolResponse:
    """原生工具调用返回的结构化结果。"""

    def __init__(
        self,
        *,
        text: str = "",
        tool_calls: list[dict] | None = None,
        finish_reason: str = "stop",
    ) -> None:
        self.text = text
        self.tool_calls = tool_calls or []
        self.finish_reason = finish_reason

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    def first_tool_call(self) -> dict | None:
        return self.tool_calls[0] if self.tool_calls else None


def _tool_dict_to_openai_schema(tool: dict) -> dict:
    """将内部工具描述转换为 OpenAI function calling 格式。"""
    params_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    for param_name, param_desc in tool.get("params", {}).items():
        if isinstance(param_desc, str):
            params_schema["properties"][param_name] = {
                "type": "string",
                "description": param_desc,
            }
        elif isinstance(param_desc, dict):
            params_schema["properties"][param_name] = param_desc
        params_schema["required"].append(param_name)
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": params_schema,
        },
    }


def build_tool_schemas(tools: dict[str, dict]) -> list[dict]:
    """将内部工具字典转换为 OpenAI 兼容的工具 Schema 列表。"""
    return [_tool_dict_to_openai_schema(t) for t in tools.values() if t.get("name")]


def chat_completion_with_tools(
    model_id: str,
    messages: list[dict[str, str]],
    tools: dict[str, dict],
    *,
    credentials: dict[str, Any] | None = None,
    max_tokens: int = 131072,
    temperature: float = 0.3,
    timeout: float = 120.0,
    max_retries: int = 3,
    response_format: dict[str, Any] | None = None,
) -> NativeToolResponse:
    """
    带原生工具调用的 chat completion — 消除 JSON 解析根源问题。
    LLM 原生返回结构化 tool_calls, 无需字符串 JSON 解析。

    Args:
        response_format: OpenAI JSON Schema 格式约束（仅 OpenAI 兼容接口支持）。
                         传入 get_react_schema() 等返回值。
    """
    import time

    endpoint = build_endpoint(model_id, credentials)
    tool_schemas = build_tool_schemas(tools)
    last_error = None

    for attempt in range(max_retries):
        try:
            if endpoint.provider == "anthropic":
                return _native_call_anthropic(
                    endpoint, messages, tool_schemas,
                    max_tokens, temperature, timeout,
                )
            else:
                return _native_call_openai_compat(
                    endpoint, messages, tool_schemas,
                    max_tokens, temperature, timeout,
                    response_format=response_format,
                )
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 429:
                wait = 2 ** attempt
                logger.warning(f"[{model_id}] 429 (native tools), 等待 {wait}s")
                time.sleep(wait)
                last_error = e
            elif 500 <= status < 600:
                wait = 2 ** attempt
                logger.warning(f"[{model_id}] {status} (native tools), 等待 {wait}s")
                time.sleep(wait)
                last_error = e
            else:
                raise
        except (
            httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
            httpx.RemoteProtocolError, httpx.WriteError, httpx.PoolTimeout,
        ) as e:
            wait = min(2 ** attempt, 8)
            logger.warning(f"[{model_id}] 网络 ({type(e).__name__}), 等待 {wait}s")
            time.sleep(wait)
            last_error = e

    raise last_error if last_error else RuntimeError("All retries failed (native tools)")


async def chat_completion_with_tools_async(
    model_id: str,
    messages: list[dict[str, str]],
    tools: dict[str, dict],
    *,
    credentials: dict[str, Any] | None = None,
    max_tokens: int = 131072,
    temperature: float = 0.3,
    timeout: float = 120.0,
    max_retries: int = 3,
) -> NativeToolResponse:
    """异步版本: 带原生工具调用的 chat completion。"""
    import asyncio

    endpoint = build_endpoint(model_id, credentials)
    tool_schemas = build_tool_schemas(tools)
    last_error = None

    for attempt in range(max_retries):
        try:
            if endpoint.provider == "anthropic":
                return await _native_call_anthropic_async(
                    endpoint, messages, tool_schemas,
                    max_tokens, temperature, timeout,
                )
            else:
                return await _native_call_openai_compat_async(
                    endpoint, messages, tool_schemas,
                    max_tokens, temperature, timeout,
                )
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 429:
                await asyncio.sleep(2 ** attempt)
                last_error = e
            elif 500 <= status < 600:
                await asyncio.sleep(2 ** attempt)
                last_error = e
            else:
                raise
        except (
            httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
            httpx.RemoteProtocolError, httpx.WriteError, httpx.PoolTimeout,
        ) as e:
            await asyncio.sleep(min(2 ** attempt, 8))
            last_error = e

    raise last_error if last_error else RuntimeError("All retries failed (native tools async)")


async def _native_call_openai_compat_async(
    endpoint, messages, tool_schemas, max_tokens, temperature, timeout,
) -> NativeToolResponse:
    """OpenAI 兼容格式的异步原生工具调用。"""
    url = _openai_compat_url(endpoint, "chat/completions")
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
    if tool_schemas:
        payload["tools"] = tool_schemas
        payload["tool_choice"] = "auto"

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    choice = data["choices"][0]
    msg = choice.get("message", {})
    text = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or msg.get("thinking") or ""
    if not text and reasoning:
        text = reasoning

    raw_tool_calls = msg.get("tool_calls") or []
    tool_calls = []
    for tc in raw_tool_calls:
        func = tc.get("function", {})
        name = func.get("name", "")
        args_str = func.get("arguments", "{}")
        try:
            args = json.loads(args_str)
        except (json.JSONDecodeError, TypeError):
            args = {}
        tool_calls.append({"id": tc.get("id", ""), "name": name, "arguments": args})

    return NativeToolResponse(text=text, tool_calls=tool_calls, finish_reason=choice.get("finish_reason", "stop"))


async def _native_call_anthropic_async(
    endpoint, messages, tool_schemas, max_tokens, temperature, timeout,
) -> NativeToolResponse:
    """Anthropic 格式的异步原生工具调用。"""
    url = f"{endpoint.base_url.rstrip('/')}/v1/messages"
    headers = {
        "x-api-key": endpoint.api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    system_msgs = [m["content"] for m in messages if m["role"] == "system"]
    chat_msgs = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]

    payload: dict[str, Any] = {
        "model": endpoint.model_name,
        "messages": chat_msgs,
        "max_tokens": max_tokens,
    }
    if system_msgs:
        payload["system"] = "\n\n".join(system_msgs)
    if tool_schemas:
        payload["tools"] = tool_schemas
    if temperature > 0:
        payload["temperature"] = temperature

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    text = ""
    tool_calls = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "arguments": block.get("input", {}),
            })

    return NativeToolResponse(
        text=text.strip() if text else "",
        tool_calls=tool_calls,
        finish_reason=data.get("stop_reason", "end_turn"),
    )


def _native_call_openai_compat(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    tool_schemas: list[dict],
    max_tokens: int,
    temperature: float,
    timeout: float,
    response_format: dict[str, Any] | None = None,
) -> NativeToolResponse:
    """OpenAI 兼容格式的原生工具调用 — 返回结构化 tool_calls。

    Args:
        response_format: 可选的 JSON Schema 输出约束。
    """
    url = _openai_compat_url(endpoint, "chat/completions")
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
    if response_format is not None:
        payload["response_format"] = response_format
    if tool_schemas:
        payload["tools"] = tool_schemas
        payload["tool_choice"] = "auto"

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    choice = data["choices"][0]
    finish = choice.get("finish_reason", "stop")
    msg = choice.get("message", {})

    text = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or msg.get("thinking") or ""
    if not text and reasoning:
        text = reasoning

    # 原生 tool_calls — 无需 JSON 字符串解析
    raw_tool_calls = msg.get("tool_calls") or []
    tool_calls = []
    for tc in raw_tool_calls:
        func = tc.get("function", {})
        name = func.get("name", "")
        args_str = func.get("arguments", "{}")
        try:
            args = json.loads(args_str)
        except (json.JSONDecodeError, TypeError):
            args = {}
        tool_calls.append({"id": tc.get("id", ""), "name": name, "arguments": args})

    if finish == "length":
        logger.warning("Native tools: 响应截断 (finish_reason=length)")

    return NativeToolResponse(text=text, tool_calls=tool_calls, finish_reason=finish)


def _native_call_anthropic(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    tool_schemas: list[dict],
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> NativeToolResponse:
    """Anthropic 原生格式的工具调用 — 使用 tool_use content block。"""
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
    }
    if system_text:
        payload["system"] = system_text
    if tool_schemas:
        payload["tools"] = [
            {
                "name": ts["function"]["name"],
                "description": ts["function"]["description"],
                "input_schema": ts["function"]["parameters"],
            }
            for ts in tool_schemas
        ]

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    text_parts = []
    tool_calls = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "arguments": block.get("input", {}),
            })

    return NativeToolResponse(
        text="\n".join(text_parts),
        tool_calls=tool_calls,
        finish_reason=data.get("stop_reason", "stop"),
    )
