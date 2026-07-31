"""Shared LLM client infrastructure: types, HTTP pool, credentials, callbacks.

Originally part of xenon.utils.llm_client — extracted to make each provider
a replaceable, independently testable module.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import logging
import httpx
import yaml
from urllib.parse import urlparse

from xenon.utils.cache_telemetry import (
    MANIFEST_RESPONSE_KEY,
    build_prompt_manifest,
)
from xenon.utils.prompt_compiler import (
    canonicalize_request_value,
    compile_prompt,
)

logger = logging.getLogger(__name__)


# B12: finish_reason=length（OpenAI 兼容）/ stop_reason=max_tokens（Anthropic）
# 时自动续写的最大次数；耗尽后抛 ResponseTruncatedError，而不是仅 logger.warning
# 后静默返回被截断的内容。
MAX_CONTINUATIONS = 3
_REASONING_EFFORTS = frozenset({"low", "medium", "high", "max"})


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
    return min(float(2 ** attempt), 30.0)


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
    """Add reasoning_effort only when the caller explicitly configured it."""
    normalized = _normalize_reasoning_effort(reasoning_effort)
    if normalized:
        payload["reasoning_effort"] = normalized


class ResponseTruncatedError(RuntimeError):
    """LLM 响应因 max_tokens 上限被截断，且续写次数耗尽仍不完整。"""


# ── §8.8.1：真实 token / 延迟统计（usage 不再丢弃）──────────────
# chat_completion 返回 str 的契约不变（向后兼容，引擎/测试均不受影响），
# 但每次成功调用经 usage 回调发出 (model_id, LLMUsage, latency)，供
# UsageTracker 等订阅。usage 在 _call_*_once 内从响应 JSON 提取并累加到
# 线程局部累加器，chat_completion 读取并发出（跨续写次数累加）。


@dataclass
class LLMUsage:
    """一次 chat_completion 累计的 LLM 调用 token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    def add(self, other: "LLMUsage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.cache_hit_tokens += other.cache_hit_tokens
        self.cache_miss_tokens += other.cache_miss_tokens


# ── R3: 结构化 LLM 响应（含原生 function-calling tool_calls） ──


@dataclass
class LLMResponse:
    """chat_completion_with_tools 的结构化返回。

    - content: 模型文本回复（可能为空，当模型仅发起 tool_call 时）
    - reasoning_content: 思考模型返回的推理内容；工具调用续轮必须保留
    - tool_calls: 原生 FC 解析出的工具调用列表，每项形如
      {"id": str, "name": str, "arguments": dict}；无工具调用时为空列表
    - finish_reason: OpenAI 风格的结束原因（stop|tool_calls|length|...）
    - usage: 本次调用的 token 用量（含缓存命中/未命中），由响应 JSON 提取
    - raw: 原始响应 JSON（调试用，可能为 None）
    """

    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)
    raw: dict[str, Any] | None = None
    provider: str = ""
    assistant_message: dict[str, Any] | None = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


def _extract_usage(data: dict[str, Any] | None, provider: str) -> LLMUsage:
    """从厂商响应 JSON 提取 usage + 缓存命中数据，归一化为 LLMUsage。

    OpenAI 兼容：``usage.{prompt,completion,total}_tokens``；
    Anthropic：``usage.{input,output}_tokens``（无 total，求和）。
    缓存字段：DeepSeek 风格的 ``prompt_cache_*``，以及 OpenAI/Ark 风格的
    ``prompt_tokens_details.cached_tokens``。
    """
    if not isinstance(data, dict):
        return LLMUsage()
    u = data.get("usage")
    if not isinstance(u, dict):
        return LLMUsage()
    if provider == "anthropic":
        p = int(u.get("input_tokens", 0) or 0)
        c = int(u.get("output_tokens", 0) or 0)
        return LLMUsage(prompt_tokens=p, completion_tokens=c, total_tokens=p + c)
    p = int(u.get("prompt_tokens", 0) or 0)
    c = int(u.get("completion_tokens", 0) or 0)
    t = u.get("total_tokens")
    # 缓存 token（DeepSeek / OpenAI 兼容字段，不存在则为 0）
    details = u.get("prompt_tokens_details")
    detail_hit = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
    hit = int(
        u.get("prompt_cache_hit_tokens", 0)
        or u.get("cache_hit_tokens", 0)
        or detail_hit
        or 0
    )
    explicit_miss = u.get("prompt_cache_miss_tokens", 0) or u.get("cache_miss_tokens", 0)
    # OpenAI-compatible responses only report cached prompt tokens. In that
    # contract, the remaining prompt tokens are the cache miss portion.
    miss = int(explicit_miss or (max(0, p - hit) if isinstance(details, dict) else 0))
    return LLMUsage(
        prompt_tokens=p, completion_tokens=c,
        total_tokens=int(t) if t else (p + c),
        cache_hit_tokens=hit, cache_miss_tokens=miss,
    )


_usage_tl = threading.local()
_USAGE_CALLBACKS: list[Any] = []
_USAGE_CB_LOCK = threading.Lock()

# 全局响应回调（供 CacheTracker 等订阅原始 API 响应，纯本地计算）
_RESPONSE_CALLBACKS: list[Any] = []
_RESPONSE_CB_LOCK = threading.Lock()


def register_response_callback(cb) -> Any:
    """注册响应回调 ``cb(model_id, response_data: dict)``。

    每次 chat_completion / chat_completion_with_tools 成功后调用，
    传入原始 API 响应 JSON。回调异常被隔离（仅告警），不影响主调用链。
    返回 unsubscribe 函数。
    """
    with _RESPONSE_CB_LOCK:
        _RESPONSE_CALLBACKS.append(cb)

    def _unsubscribe() -> None:
        with _RESPONSE_CB_LOCK:
            try:
                _RESPONSE_CALLBACKS.remove(cb)
            except ValueError:
                pass
    return _unsubscribe


def _emit_response(model_id: str, data: dict[str, Any]) -> None:
    """向所有响应回调发送原始 API 响应数据。"""
    with _RESPONSE_CB_LOCK:
        cbs = list(_RESPONSE_CALLBACKS)
    for cb in cbs:
        try:
            cb(model_id, data)
        except Exception:
            logger.warning("响应回调执行异常（已隔离）", exc_info=True)


def _set_cache_manifest(
    model_id: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    request_shape: dict[str, Any] | None = None,
    prompt_layout: dict[str, Any] | None = None,
    cache_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe the current request without retaining its original content."""
    manifest = build_prompt_manifest(
        model_id,
        messages,
        tools=tools,
        request_shape=request_shape,
        prompt_layout=prompt_layout,
        cache_context=cache_context,
    ).as_dict()
    _usage_tl.cache_manifest = manifest
    return manifest


def _response_with_manifest(
    data: dict[str, Any],
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach local-only attribution metadata to a callback copy."""
    payload = dict(data)
    current = manifest or getattr(_usage_tl, "cache_manifest", None)
    if current:
        payload[MANIFEST_RESPONSE_KEY] = dict(current)
    return payload


def _prepare_cache_lane(
    registry: Any,
    model_id: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    request_shape: dict[str, Any] | None = None,
    cache_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Attach exact-prefix lane diagnostics after deterministic compilation."""
    if registry is None:
        return cache_context
    context = dict(cache_context or {})
    try:
        decision = registry.prepare(
            model_id,
            str(context.get("engine") or "utility"),
            str(context.get("phase") or "request"),
            int(context.get("context_epoch") or 0),
            messages,
            tools=tools,
            request_shape=request_shape,
            event_cursor=int(context.get("event_cursor") or 0),
        )
        context.update(decision.cache_context())
    except Exception:
        # Cache telemetry must never make an otherwise valid provider request
        # fail. The request proceeds without lane attribution.
        logger.warning("缓存轨道记录失败（已隔离）", exc_info=True)
    return context


def register_usage_callback(cb) -> Any:
    """注册 usage 回调 ``cb(model_id, usage: LLMUsage, latency: float)``。

    返回 unsubscribe 函数。回调异常被隔离（仅告警），不影响主调用链。
    """
    with _USAGE_CB_LOCK:
        _USAGE_CALLBACKS.append(cb)

    def _unsubscribe() -> None:
        with _USAGE_CB_LOCK:
            try:
                _USAGE_CALLBACKS.remove(cb)
            except ValueError:
                pass

    return _unsubscribe


def _emit_usage(model_id: str, usage: LLMUsage, latency: float) -> None:
    with _USAGE_CB_LOCK:
        cbs = list(_USAGE_CALLBACKS)
    for cb in cbs:
        try:
            cb(model_id, usage, latency)
        except Exception:
            logger.warning("usage 回调执行异常（已隔离）", exc_info=True)


def _acc_usage(provider: str, data: dict[str, Any] | None, model_id: str = "") -> None:
    """把单次响应的 usage 累加到当前线程累加器，并发出响应回调。"""
    acc = getattr(_usage_tl, "usage_acc", None)
    if acc is not None:
        acc.add(_extract_usage(data, provider))
    # 发出响应回调（供 CacheTracker 等订阅原始 API 响应）
    if isinstance(data, dict):
        _emit_response(model_id, _response_with_manifest(data))


@dataclass
class _UsageTotals:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    latency_sum: float = 0.0


class UsageTracker:
    """累计 LLM 调用的真实 token / 延迟统计（订阅 usage 回调）。

    用法：``tracker = UsageTracker()`` 后，所有 ``chat_completion`` 的真实
    usage 都会被累计；``snapshot()`` 取各模型统计，``total_tokens()`` 取总
    token，``close()`` 取消订阅。
    """

    def __init__(self) -> None:
        self._totals: dict[str, _UsageTotals] = {}
        self._lock = threading.Lock()
        self._unsubscribe = register_usage_callback(self._on_usage)

    def _on_usage(self, model_id: str, usage: LLMUsage, latency: float) -> None:
        with self._lock:
            t = self._totals.setdefault(model_id, _UsageTotals())
            t.calls += 1
            t.prompt_tokens += usage.prompt_tokens
            t.completion_tokens += usage.completion_tokens
            t.total_tokens += usage.total_tokens
            t.cache_hit_tokens += usage.cache_hit_tokens
            t.cache_miss_tokens += usage.cache_miss_tokens
            t.latency_sum += latency

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                m: {
                    "calls": t.calls,
                    "prompt_tokens": t.prompt_tokens,
                    "completion_tokens": t.completion_tokens,
                    "total_tokens": t.total_tokens,
                    "cache_hit_tokens": t.cache_hit_tokens,
                    "cache_miss_tokens": t.cache_miss_tokens,
                    "latency_avg": (t.latency_sum / t.calls) if t.calls else 0.0,
                }
                for m, t in self._totals.items()
            }

    def total_tokens(self) -> int:
        with self._lock:
            return sum(t.total_tokens for t in self._totals.values())

    def total_calls(self) -> int:
        with self._lock:
            return sum(t.calls for t in self._totals.values())

    def close(self) -> None:
        self._unsubscribe()


# ── per-provider 长生命 httpx Client 池（R3 / §8.4.3 / §8.9.4） ──
# 消除 chat_completion 每次调用新建+销毁 Client 的开销（同 provider 10+ 次
# 调用不再各做一次完整 TLS 握手）。httpx.Client 本身线程安全，可被多线程
# 并发复用；池以 (provider, base_url) 为键，proxy/timeout 在创建时固定，
# 单次请求可通过 client.post(..., timeout=) 覆盖超时。
_CLIENT_POOL: dict[str, httpx.Client] = {}
_CLIENT_LOCK = threading.Lock()


def _client_pool_key(endpoint: "ModelEndpoint") -> str:
    return f"{endpoint.provider}|{endpoint.base_url}"


def _get_pooled_client(endpoint: "ModelEndpoint", timeout: float = 120.0) -> httpx.Client:
    """获取（或创建）per-provider 复用的长生命 httpx.Client。"""
    key = _client_pool_key(endpoint)
    with _CLIENT_LOCK:
        client = _CLIENT_POOL.get(key)
        if client is None or client.is_closed:
            client = _create_http_client(timeout=timeout)
            _CLIENT_POOL[key] = client
        return client


def close_clients() -> None:
    """显式关闭所有池化 Client（进程退出或测试清理时调用）。"""
    with _CLIENT_LOCK:
        for client in _CLIENT_POOL.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001 — 关闭时忽略个别异常
                pass
        _CLIENT_POOL.clear()


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
_CREDENTIALS_PATH = Path.home() / ".xenon" / "credentials.yaml"


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
    "ark": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "env_key": "ARK_API_KEY",
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
    """从 ~/.xenon/credentials.yaml 或环境变量加载 API Key。

    v0.3.0+ 修复（C-2 延伸）：anthropic 厂商额外 fallback ANTHROPIC_AUTH_TOKEN
    （Claude Code / Anthropic SDK 标准环境变量）。原来只认 ANTHROPIC_API_KEY，
    导致 Claude Code 内跑 xenon 走代理（如火山方舟）时即便 ANTHROPIC_AUTH_TOKEN
    已设也会报"未找到 API Key"。
    """
    creds: dict[str, str] = {}
    if _CREDENTIALS_PATH.exists():
        with open(_CREDENTIALS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                for key, value in data.items():
                    normalized_key = str(key).strip().lower()
                    if not normalized_key:
                        continue
                    if isinstance(value, str):
                        value = value.strip()
                        if not value:
                            continue
                    # Keep the legacy custom-provider mapping available to
                    # build_endpoint while rejecting blank scalar secrets.
                    creds[normalized_key] = value
            else:
                logger.warning("凭证文件不是 YAML 映射，忽略其内容: %s", _CREDENTIALS_PATH)
                data = {}
            if not creds.get("ark"):
                legacy_key = _legacy_ark_api_key(data)
                if legacy_key:
                    creds["ark"] = legacy_key

    # v0.4.0 修复: 环境变量作为补充（yaml 优先，env 仅在 yaml 未配置时生效）
    # 此前无条件覆盖导致 ~/.bashrc 旧 key 覆盖 yaml 新 key，对齐 provider_registry 行为
    for provider, cfg in _PROVIDER_DEFAULTS.items():
        env_val = os.getenv(cfg["env_key"])
        if isinstance(env_val, str) and env_val.strip() and not creds.get(provider):
            creds[provider] = env_val.strip()

    # v0.3.0+ 修复（C-2）：anthropic 额外 fallback ANTHROPIC_AUTH_TOKEN
    if not creds.get("anthropic"):
        auth_token = os.getenv("ANTHROPIC_AUTH_TOKEN")
        if isinstance(auth_token, str) and auth_token.strip():
            creds["anthropic"] = auth_token.strip()
    return creds


def _legacy_ark_api_key(data: dict[str, Any]) -> str:
    """Read one unambiguous Ark key from the pre-0.7 custom-provider shape."""
    custom = data.get("_custom_providers", {})
    if not isinstance(custom, dict):
        return ""
    candidates: list[str] = []
    for config in custom.values():
        if not isinstance(config, dict):
            continue
        try:
            hostname = (urlparse(str(config.get("base_url", ""))).hostname or "").lower()
        except ValueError:
            continue
        api_key = config.get("api_key")
        if hostname == "ark.cn-beijing.volces.com" and isinstance(api_key, str) and api_key.strip():
            candidates.append(api_key.strip())
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else ""


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



def _load_custom_provider_config(provider_key: str) -> dict | None:
    """v0.4.0: 从 credentials.yaml 加载自定义模型商配置。

    v0.5.3: 兼容旧版本产生的空 key（纯中文名称注册时 key 被清空）。
    查找顺序：exact key → "custom"（修补后的默认 key）→ 空字符串（旧版本遗留）。
    """
    try:
        import yaml as _yaml
        path = Path.home() / ".xenon" / "credentials.yaml"
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            data = _yaml.safe_load(f) or {}
        custom_providers = data.get("_custom_providers", {})
        # v0.5.3: 兼容空 key 和修补后的 "custom" key
        cfg = (
            custom_providers.get(provider_key)
            or custom_providers.get("custom")
            or custom_providers.get("")
        )
        # 如果通过空 key 找到，自动修复为 "custom"（下次保存时生效）
        if cfg is not None and not custom_providers.get(provider_key):
            custom_providers["custom"] = cfg
        return cfg
    except Exception:
        return None


def build_endpoint(model_id: str, credentials: dict[str, str] | None = None, base_url: str | None = None) -> ModelEndpoint:
    """根据 model_id 构建完整的调用端点信息。

    v0.4.0: 支持动态注册的自定义模型商。
    """
    provider, model_name = parse_model_id(model_id)
    creds = credentials or _load_credentials()

    # v0.4.0: 先查内置 + 动态注册的 defaults
    defaults = _PROVIDER_DEFAULTS.get(provider)
    custom_config = None
    if defaults is None:
        # 尝试从自定义模型商加载
        custom_config = _load_custom_provider_config(provider)
        if custom_config:
            defaults = {"base_url": custom_config["base_url"],
                        "env_key": "", "max_output_tokens": 8192}
        else:
            raise ValueError(
                f"不支持的 provider: {provider}，内置: {list(_PROVIDER_DEFAULTS.keys())}。"
                f"可使用 /setup 注册自定义模型商。"
            )

    api_key = creds.get(provider, "")
    api_key = api_key.strip() if isinstance(api_key, str) else ""
    # v0.5.3: 自定义模型商的 API Key 优先从 custom_config 取
    if not api_key and custom_config:
        api_key = custom_config.get("api_key", "")
        api_key = api_key.strip() if isinstance(api_key, str) else ""
    if not api_key:
        raise ValueError(
            f"未找到 {provider} 的 API Key。"
            f"请在 {_CREDENTIALS_PATH} 或环境变量 {defaults.get('env_key', '')} 中配置。"
        )
    return ModelEndpoint(
        provider=provider,
        model_name=model_name,
        base_url=(
            base_url
            or os.getenv(f"{provider.upper()}_BASE_URL")
            or defaults["base_url"]
        ),
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
    # B4: 按厂商输出上限钳制 max_tokens，防止 131072 等超限值引发 400 级联失败
    provider_cap = _PROVIDER_DEFAULTS.get(endpoint.provider, {}).get("max_output_tokens")
    if provider_cap and max_tokens > provider_cap:
        max_tokens = provider_cap
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
                text = _call_anthropic(endpoint, messages, max_tokens, temperature, timeout)
            else:
                if reasoning_effort:
                    text = _call_openai_compat(
                        endpoint, messages, max_tokens, temperature, timeout,
                        reasoning_effort=reasoning_effort,
                    )
                else:
                    text = _call_openai_compat(
                        endpoint, messages, max_tokens, temperature, timeout,
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
                logger.warning(f"[{model_id}] 429 限流，等待 {wait:g}s 后重试 (第 {attempt + 1}/{attempt_count} 次)")
                time.sleep(wait)
            elif 500 <= status < 600:
                last_error = e
                if attempt + 1 >= attempt_count:
                    break
                # 服务端错误 — 指数退避重试
                wait = _retry_delay(e, attempt)
                logger.warning(f"[{model_id}] {status} 服务端错误，等待 {wait:g}s 后重试 (第 {attempt + 1}/{attempt_count} 次)")
                time.sleep(wait)
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
            last_error = e
            if attempt + 1 >= attempt_count:
                break
            wait = min(2 ** attempt, 8)
            logger.warning(f"[{model_id}] 网络错误 ({type(e).__name__}): {e}，等待 {wait}s 后重试 (第 {attempt + 1}/{attempt_count} 次)")
            time.sleep(wait)

    # 所有重试都失败
    raise last_error


