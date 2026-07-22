"""
DeepSeek Cache Tracker — 本地缓存命中率 + 费用追踪器。

设计原则：
- 所有数据来自 API 响应 JSON 的 ``usage.*_tokens`` 字段，零额外 LLM 调用。
- 费用 = token × 本地定价表（本地乘法，不调任何远程 API）。
- system_prompt 变更检测 = SHA256 hash（本地计算）。
- 命中率骤降检测 = 滚动窗口对比（纯内存计算）。

用法::

    tracker = CacheTracker()
    # ... LLM 调用中自动通过 usage 回调记录 ...
    print(tracker.cache_hit_rate)          # "88.3%"
    print(tracker.estimated_cost_yuan)     # 0.089
    print(tracker.savings_yuan)            # 1.15 (比全 miss 省的钱)
    tracker.close()
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from xenon.utils.cache_telemetry import (
    MANIFEST_RESPONSE_KEY,
    CacheEvent,
    CacheEventStore,
    build_cache_event,
    configure_persistent_secret,
)


# ══════════════════════════════════════════════════════════════
# DeepSeek 定价表（核对日期：2026-07-21，来自官方中文 API 文档）
# 单位：元 / 百万 tokens
# https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
# ══════════════════════════════════════════════════════════════

# 默认定价（当无法匹配具体模型时使用 V4-Pro 定价）
_DEFAULT_PRICING = {
    "input_cache_hit": 0.025,    # ¥0.025 / 1M tokens
    "input_cache_miss": 3.0,     # ¥3 / 1M tokens
    "output": 6.0,               # ¥6 / 1M tokens
}

# 按模型细分的定价
_MODEL_PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-pro": _DEFAULT_PRICING,
    "deepseek-v4-flash": {
        "input_cache_hit": 0.02,
        "input_cache_miss": 1.0,
        "output": 2.0,
    },
}

# 旧别名在 2026-07-24 23:59（北京时间）前映射到 V4 Flash；保留价格
# 映射只为历史 usage 账单，不再把它们暴露为可选模型。
_LEGACY_MODEL_ALIASES = {
    "deepseek-chat": "deepseek-v4-flash",
    "deepseek-reasoner": "deepseek-v4-flash",
}


def _canonical_model_id(model_id: str) -> str:
    """Return one stable accounting key across streaming and engine paths.

    Streaming calls report the registry ID (``deepseek/model``), while the
    blocking client historically reported only the provider-side model name.
    DeepSeek names are unambiguous, so prefix bare names with their provider
    and normalize casing.  Other providers keep their explicit key unchanged.
    """
    key = str(model_id or "").strip().lower()
    if not key:
        return "unknown"
    if "/" in key:
        provider, name = key.split("/", 1)
        return f"{provider}/{name}"
    if key.startswith("deepseek-"):
        return f"deepseek/{key}"
    return key


def _match_pricing(model_id: str) -> dict[str, float]:
    """根据 model_id 匹配当前官方定价；未知模型保守按 V4 Pro 估算。"""
    key = model_id.lower().rsplit("/", 1)[-1].replace("_", "-")
    key = _LEGACY_MODEL_ALIASES.get(key, key)
    if key in _MODEL_PRICING:
        return _MODEL_PRICING[key]
    for name, pricing in _MODEL_PRICING.items():
        if name in key:
            return pricing
    return _DEFAULT_PRICING


@dataclass
class CacheSnapshot:
    """单次 API 调用的缓存数据快照。"""
    timestamp: float
    model_id: str
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int        # 缓存放大的 prompt token
    cache_miss_tokens: int       # 缓存未放大的 prompt token
    system_hash: str             # 调用时的 system prompt hash


@dataclass
class _ModelTotals:
    """单模型累计统计。"""
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    cache_reported_calls: int = 0
    # 费用
    input_cache_hit_cost: float = 0.0
    input_cache_miss_cost: float = 0.0
    output_cost: float = 0.0
    # 历史快照（滚动窗口，最近 50 次）
    window: deque = field(default_factory=lambda: deque(maxlen=50))


class CacheTracker:
    """DeepSeek 缓存命中率 + 本地费用追踪。

    通过订阅现有 usage 回调来获取每次 LLM 调用的 token 数据，
    并从原始 API 响应中额外提取缓存命中/未命中 token 数。

    所有费用计算完全在本地完成——不调任何远程 API。
    """

    # 命中率骤降阈值
    DROP_THRESHOLD = 0.40   # 相较近期平均值下降 40% 以上触发告警
    MIN_WINDOW_SIZE = 5     # 滚动窗口至少 5 次调用才开始检测
    NORMAL_HIT_RATE = 0.70  # "正常"命中率下限

    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        persist: bool = False,
        event_store: CacheEventStore | None = None,
    ) -> None:
        # per-model 累计
        self._models: dict[str, _ModelTotals] = {}
        self._lock = threading.Lock()
        self._events: deque[CacheEvent] = deque(maxlen=200)
        self._family_calls: dict[str, int] = {}
        self._event_store = event_store or (CacheEventStore() if persist else None)
        if self._event_store is not None:
            try:
                configure_persistent_secret(self._event_store.directory)
            except (OSError, ValueError):
                # Continue with the process-local key when private storage is
                # unavailable or a user-managed key is malformed.
                pass

        # system prompt 变更跟踪
        self._system_hash: str = ""
        self._system_hash_history: list[tuple[float, str]] = []  # [(timestamp, hash), ...]

        # 全局定价覆盖（从 YAML 加载）
        self._custom_pricing: dict[str, dict[str, float]] = {}
        if config_path:
            self._load_pricing(config_path)

        # 当前 system prompt（由外部设置，供 _on_response 使用）
        self._current_system_prompt: str = ""

        # 订阅全局 API 响应回调——每次 LLM 调用后自动提取缓存数据
        from xenon.utils.llm_client import register_response_callback
        self._unsubscribe = register_response_callback(self._on_response)

    # ── 响应回调 ─────────────────────────────────────────

    def _on_response(self, model_id: str, data: dict[str, Any]) -> None:
        """全局响应回调——从原始 API 响应 JSON 提取缓存数据。
        完全不调用 LLM API，只解析已返回的 JSON。"""
        self.record_response(model_id, data, self._current_system_prompt)

    def set_system_prompt(self, prompt: str) -> None:
        """设置当前 system prompt（供 hash 追踪）。"""
        self._current_system_prompt = prompt

    # ── 定价加载 ───────────────────────────────────────────

    def _load_pricing(self, path: str | Path) -> None:
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for model_id, p in data.items():
                if isinstance(p, dict):
                    self._custom_pricing[str(model_id).lower()] = {
                        "input_cache_hit": float(p.get("input_cache_hit", 0.025)),
                        "input_cache_miss": float(p.get("input_cache_miss", 3.0)),
                        "output": float(p.get("output", 6.0)),
                    }
        except Exception:
            pass  # 配置文件不存在或格式错误时使用默认定价

    def get_pricing(self, model_id: str) -> dict[str, float]:
        """获取模型定价（自定义 > 内置 > 默认）。"""
        key = model_id.lower().replace("deepseek/", "")
        if key in self._custom_pricing:
            return self._custom_pricing[key]
        return _match_pricing(model_id)

    # ── 核心记录 ─────────────────────────────────────────

    def record_response(self, model_id: str, response_data: dict[str, Any], system_prompt: str = "") -> None:
        """从 API 原始响应 JSON 中提取缓存 + token 数据并累计。

        应在每次 chat_completion / chat_completion_with_tokens 返回后立即调用。
        完全不上行任何网络请求——仅解析已返回的 JSON。
        """
        usage_data = response_data.get("usage") if isinstance(response_data, dict) else None
        if not isinstance(usage_data, dict):
            return

        # 区分“厂商明确返回 0”与“厂商完全没有缓存字段”。后者不能展示成 0%。
        cache_field_names = {
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
            "cache_hit_tokens",
            "cache_miss_tokens",
        }
        cache_fields_present = any(name in usage_data for name in cache_field_names)

        # 提取缓存 token
        cache_hit = int(usage_data.get("prompt_cache_hit_tokens", 0)
                        or usage_data.get("cache_hit_tokens", 0)
                        or 0)
        cache_miss = int(usage_data.get("prompt_cache_miss_tokens", 0)
                         or usage_data.get("cache_miss_tokens", 0)
                         or 0)

        # 基础 token 数
        prompt = int(usage_data.get("prompt_tokens", 0)
                     or usage_data.get("input_tokens", 0)
                     or 0)
        completion = int(usage_data.get("completion_tokens", 0)
                         or usage_data.get("output_tokens", 0)
                         or 0)
        model_id = _canonical_model_id(model_id)

        manifest = response_data.get(MANIFEST_RESPONSE_KEY)
        if not isinstance(manifest, dict):
            manifest = None
        family = str((manifest or {}).get("cache_family") or f"legacy:{model_id}")

        # system prompt hash
        sys_hash = _hash_system_prompt(system_prompt) if system_prompt else ""

        with self._lock:
            t = self._models.setdefault(model_id, _ModelTotals())
            t.calls += 1
            t.prompt_tokens += prompt
            t.completion_tokens += completion
            t.cache_hit_tokens += cache_hit
            t.cache_miss_tokens += cache_miss
            if cache_fields_present:
                t.cache_reported_calls += 1

            family_call = self._family_calls.get(family, 0) + 1
            self._family_calls[family] = family_call

            # 费用计算（纯本地乘法）
            pricing = self.get_pricing(model_id)
            t.input_cache_hit_cost += (cache_hit / 1_000_000) * pricing["input_cache_hit"]
            t.input_cache_miss_cost += (cache_miss / 1_000_000) * pricing["input_cache_miss"]
            t.output_cost += (completion / 1_000_000) * pricing["output"]

            # 记录快照到滚动窗口
            snap = CacheSnapshot(
                timestamp=time.monotonic(),
                model_id=model_id,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cache_hit_tokens=cache_hit,
                cache_miss_tokens=cache_miss,
                system_hash=sys_hash,
            )
            t.window.append(snap)

            event = build_cache_event(
                manifest,
                model_id=model_id,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cache_hit_tokens=cache_hit,
                cache_miss_tokens=cache_miss,
                cache_fields_present=cache_fields_present,
                family_call=family_call,
                previous_event=self._events[-1] if self._events else None,
            )
            self._events.append(event)

        if self._event_store is not None:
            try:
                self._event_store.append(event)
            except OSError:
                # Telemetry must never make an otherwise valid model call fail.
                pass

        # 跟踪 system hash 变化
        if sys_hash and sys_hash != self._system_hash:
            self._system_hash_history.append((time.monotonic(), sys_hash))
            self._system_hash = sys_hash

    # ── 聚合查询 ───────────────────────────────────────────

    @property
    def cache_hits(self) -> int:
        """总缓存命中 token 数。"""
        with self._lock:
            return sum(t.cache_hit_tokens for t in self._models.values())

    @property
    def cache_misses(self) -> int:
        """总缓存未命中 token 数。"""
        with self._lock:
            return sum(t.cache_miss_tokens for t in self._models.values())

    @property
    def cache_hit_rate(self) -> float:
        """全局缓存命中率 (0.0 ~ 1.0)。"""
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total > 0 else 0.0

    @property
    def cache_hit_rate_pct(self) -> str:
        """缓存命中率百分比字符串。"""
        return f"{self.cache_hit_rate * 100:.1f}%"

    @property
    def estimated_cost_yuan(self) -> float:
        """预估总费用（元）。"""
        with self._lock:
            total = 0.0
            for t in self._models.values():
                total += t.input_cache_hit_cost + t.input_cache_miss_cost + t.output_cost
            return round(total, 4)

    @property
    def estimated_cost_display(self) -> str:
        """费用展示字符串。"""
        cost = self.estimated_cost_yuan
        if cost < 0.01:
            return "¥<0.01"
        elif cost < 0.10:
            return f"¥{cost:.3f}"
        else:
            return f"¥{cost:.2f}"

    @property
    def savings_yuan(self) -> float:
        """如果全部 prompt token 都是 miss 的费用 - 实际费用。"""
        with self._lock:
            total_actual = 0.0
            total_if_all_miss = 0.0
            for model_id, t in self._models.items():
                pricing = self.get_pricing(model_id)
                # 实际费用
                total_actual += t.input_cache_hit_cost + t.input_cache_miss_cost + t.output_cost
                # 假设全部 miss
                all_prompt = t.cache_hit_tokens + t.cache_miss_tokens
                if_miss = (all_prompt / 1_000_000) * pricing["input_cache_miss"]
                total_if_all_miss += if_miss + t.output_cost
            return round(max(0, total_if_all_miss - total_actual), 4)

    @property
    def savings_pct(self) -> int:
        """节省百分比（整数），如 92 表示省了 92%。"""
        total = self.estimated_cost_yuan + self.savings_yuan
        if total <= 0:
            return 0
        return int(self.savings_yuan / total * 100)

    def model_snapshot(self, model_id: str) -> dict[str, Any]:
        """单个模型的快照（用于 /cost 面板）。"""
        model_id = _canonical_model_id(model_id)
        with self._lock:
            t = self._models.get(model_id)
            if not t:
                return {}
            hit_rate = t.cache_hit_tokens / (t.cache_hit_tokens + t.cache_miss_tokens) \
                       if (t.cache_hit_tokens + t.cache_miss_tokens) > 0 else 0.0
            total_cost = t.input_cache_hit_cost + t.input_cache_miss_cost + t.output_cost
            all_prompt = t.cache_hit_tokens + t.cache_miss_tokens
            pricing = self.get_pricing(model_id)
            if_all_miss = (all_prompt / 1_000_000) * pricing["input_cache_miss"]
            saved = if_all_miss + t.output_cost - total_cost

            return {
                "calls": t.calls,
                "prompt_tokens": t.prompt_tokens,
                "completion_tokens": t.completion_tokens,
                "cache_hit_tokens": t.cache_hit_tokens,
                "cache_miss_tokens": t.cache_miss_tokens,
                "cache_hit_rate": hit_rate,
                "cache_reported_calls": t.cache_reported_calls,
                "cache_field_coverage": (
                    t.cache_reported_calls / t.calls if t.calls else 0.0
                ),
                "cost_yuan": round(total_cost, 4),
                "saved_yuan": round(max(0, saved), 4),
            }

    @property
    def all_models(self) -> list[str]:
        with self._lock:
            return list(self._models.keys())

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent privacy-safe per-request telemetry, newest last."""
        with self._lock:
            return [event.as_dict() for event in list(self._events)[-max(0, limit):]]

    def stored_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Load privacy-safe cross-session history when persistence is enabled."""
        if self._event_store is None:
            return self.recent_events(limit)
        return self._event_store.load(limit=max(0, limit))

    @property
    def total_calls(self) -> int:
        with self._lock:
            return sum(total.calls for total in self._models.values())

    @property
    def cache_reported_calls(self) -> int:
        with self._lock:
            return sum(total.cache_reported_calls for total in self._models.values())

    @property
    def cache_field_coverage(self) -> float:
        calls = self.total_calls
        return self.cache_reported_calls / calls if calls else 0.0

    @property
    def latest_event(self) -> dict[str, Any] | None:
        with self._lock:
            return self._events[-1].as_dict() if self._events else None

    @property
    def cache_badge(self) -> tuple[str, str]:
        """Return ``(text, semantic_style)`` without treating unknown as 0%."""
        event = self.latest_event
        if event is None:
            return "cache cold", "muted"
        state = event["state"]
        if state == "unavailable":
            return "cache n/a", "muted"
        if state == "cold":
            return "cache cold", "warning"
        if state == "warming":
            return "cache warming", "warning"
        if state == "miss" and self.cache_hits == 0:
            return "cache 0% ↓", "danger"
        return f"cache {self.cache_hit_rate:.0%}", "good"

    def diagnostics(self) -> list[dict[str, str]]:
        """Return deterministic local cache checks for ``/cache doctor``."""
        events = self.recent_events(20)
        checks: list[dict[str, str]] = []
        if not events:
            return [{
                "level": "info",
                "name": "观测样本",
                "detail": "尚无模型响应；当前是 cold，不代表 0% 命中。",
            }]
        coverage = self.cache_field_coverage
        checks.append({
            "level": "ok" if coverage == 1.0 else "warn",
            "name": "缓存字段",
            "detail": f"厂商在 {self.cache_reported_calls}/{self.total_calls} 次响应中返回缓存字段（{coverage:.0%}）。",
        })
        families = {event["cache_family"] for event in events[-10:]}
        churn = len(families) / min(10, len(events))
        if len(events) < 3:
            family_level = "info"
            family_detail = (
                f"当前仅 {len(events)} 个样本，至少 3 个样本后评估稳定性。"
            )
        else:
            family_level = "ok" if churn <= 0.4 else "warn"
            family_detail = (
                f"最近 {min(10, len(events))} 次请求使用 {len(families)} 个缓存族。"
            )
        checks.append({
            "level": family_level,
            "name": "缓存族稳定性",
            "detail": family_detail,
        })
        latest = events[-1]
        compiler_warnings = latest.get("compiler_warnings") or []
        if any(str(item).startswith("dynamic_stable_system:") for item in compiler_warnings):
            checks.append({
                "level": "warn",
                "name": "稳定前缀动态内容",
                "detail": "固定 system 区检测到日期/时间等动态内容，建议移至当前请求层。",
            })
        efficiency = latest.get("prefix_efficiency")
        if efficiency is not None:
            checks.append({
                "level": "ok" if efficiency >= 0.5 else "warn",
                "name": "前缀效率",
                "detail": f"最近请求实际命中/预期可缓存 token 约为 {efficiency:.0%}。",
            })
        if self._event_store is not None:
            checks.append({
                "level": "ok",
                "name": "本地历史",
                "detail": f"仅保存哈希与计数：{self._event_store.path}",
            })
        return checks

    def family_snapshot(self, cache_family: str) -> dict[str, Any]:
        """Summarize the current session's observations for one cache family."""
        with self._lock:
            events = [event for event in self._events if event.cache_family == cache_family]
        if not events:
            return {}
        hit = sum(event.cache_hit_tokens for event in events)
        miss = sum(event.cache_miss_tokens for event in events)
        total = hit + miss
        return {
            "cache_family": cache_family,
            "calls": len(events),
            "model_id": events[-1].model_id,
            "engine": events[-1].engine,
            "phase": events[-1].phase,
            "state": events[-1].state,
            "cause": events[-1].cause,
            "cache_hit_rate": hit / total if total else None,
        }

    # ── 命中率骤降检测 ─────────────────────────────────────

    def check_hit_rate_drop(self) -> dict[str, Any] | None:
        """检测缓存命中率是否骤降。

        如果滚动窗口平均值相较前一段窗口下降超过 DROP_THRESHOLD，
        返回告警信息；否则返回 None。
        """
        with self._lock:
            # 取所有模型的整体窗口
            all_snaps: list[CacheSnapshot] = []
            for t in self._models.values():
                all_snaps.extend(t.window)
            all_snaps.sort(key=lambda s: s.timestamp)

            if len(all_snaps) < self.MIN_WINDOW_SIZE:
                return None

            n = len(all_snaps)
            half = max(self.MIN_WINDOW_SIZE, n // 2)
            recent = all_snaps[-half:]
            older = all_snaps[:half]

            recent_hit = sum(s.cache_hit_tokens for s in recent)
            recent_miss = sum(s.cache_miss_tokens for s in recent)
            recent_total = recent_hit + recent_miss
            recent_rate = recent_hit / recent_total if recent_total > 0 else 0.0

            older_hit = sum(s.cache_hit_tokens for s in older)
            older_miss = sum(s.cache_miss_tokens for s in older)
            older_total = older_hit + older_miss
            older_rate = older_hit / older_total if older_total > 0 else 0.0

            if older_rate > 0:  # 避免除零
                drop = (older_rate - recent_rate) / older_rate
            else:
                drop = 0.0

            if drop > self.DROP_THRESHOLD and recent_rate < self.NORMAL_HIT_RATE:
                return {
                    "recent_rate": recent_rate,
                    "older_rate": older_rate,
                    "drop_pct": drop,
                    "recent_samples": half,
                    "older_samples": half,
                    "suggestion": self._suggest_fix(),
                }
            return None

    def _suggest_fix(self) -> str:
        """根据 system hash 变化历史给出建议。"""
        if len(self._system_hash_history) >= 2:
            last_change = self._system_hash_history[-1]
            time_ago = time.monotonic() - last_change[0]
            if time_ago < 300:  # 5 分钟内变更过
                return (
                    "system prompt 近期发生变动（{} 秒前），"
                    "这正是命中率下降的原因。建议恢复原来的 system prompt "
                    "或将变动部分移至 user 消息中。使用 /cache doctor 检查。"
                ).format(int(time_ago))
        return (
            "命中率下降可能是因为 prompt 结构发生了变化。"
            "使用 /cache explain 和 /cache doctor 查看本地证据。"
        )

    # ── system prompt 管理 ──────────────────────────────────

    @staticmethod
    def hash_prompt(prompt: str) -> str:
        return _hash_system_prompt(prompt)

    @property
    def system_hash(self) -> str:
        return self._system_hash

    def close(self) -> None:
        if hasattr(self, '_unsubscribe'):
            self._unsubscribe()


def _hash_system_prompt(prompt: str) -> str:
    """计算 system prompt 的 SHA256 摘要（前 16 字符）用于变更检测。"""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
