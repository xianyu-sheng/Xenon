"""Usage tracking and response callbacks (extracted from llm_client)."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any


from xenon.utils.cache_telemetry import MANIFEST_RESPONSE_KEY, build_prompt_manifest

logger = logging.getLogger(__name__)


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
    explicit_miss = u.get("prompt_cache_miss_tokens", 0) or u.get(
        "cache_miss_tokens", 0
    )
    # OpenAI-compatible responses only report cached prompt tokens. In that
    # contract, the remaining prompt tokens are the cache miss portion.
    miss = int(explicit_miss or (max(0, p - hit) if isinstance(details, dict) else 0))
    return LLMUsage(
        prompt_tokens=p,
        completion_tokens=c,
        total_tokens=int(t) if t else (p + c),
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
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
    """向所有响应回调发送原始 API 响应数据。

    P1: 迭代前在锁内复制回调列表，避免与 register/unregister 并发时的竞态条件。
    回调在锁外执行，避免回调内部操作导致死锁。
    """
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
    """发送 usage 数据到所有注册的回调。

    P1: 迭代前在锁内复制回调列表，避免与 register/unregister 并发时的竞态条件。
    回调在锁外执行，避免回调内部操作导致死锁。
    """
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


