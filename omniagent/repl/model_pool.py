"""
v0.4.0: 加权模型池 + 健康追踪 + 断路器。

替代 role_priority 列表，提供：
- CapabilityProfile: 从模型名推断能力画像
- HealthRecord: 运行时成功率、延迟、断路器状态
- ModelPool: 加权选择 + 健康感知路由
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CapabilityProfile:
    """从模型名推断的能力画像（静态，注册时生成）."""
    tier: int = 3                # 1 (cheapest) to 5 (flagship)
    reasoning_score: float = 0.4
    coding_score: float = 0.4
    tool_use_score: float = 0.5
    cost_efficiency: float = 0.5
    context_window: int = 128000
    supports_tools: bool = True
    supports_streaming: bool = True


@dataclass
class HealthRecord:
    """运行时健康指标（动态，每次 API 调用更新）."""
    total_calls: int = 0
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    avg_latency: float = 0.0
    last_latencies: list[float] = field(default_factory=list)
    circuit_open_until: float = 0.0          # timestamp, 0 = closed
    last_used_at: float = 0.0


@dataclass
class PoolEntry:
    """模型池中的一个模型条目."""
    model_id: str          # "deepseek/deepseek-v4-pro"
    alias: str             # "deepseek-v4-pro"
    weight: float = 1.0    # user-configured priority
    capability: CapabilityProfile = field(default_factory=CapabilityProfile)
    health: HealthRecord = field(default_factory=HealthRecord)
    api_key: str = ""
    base_url: str = ""


FAILURE_THRESHOLD = 3
COOLDOWN_BASE = 30.0
MAX_COOLDOWN = 600.0


class ModelPool:
    """加权模型池 + 健康追踪 + 断路器。

    线程安全，支持并发更新。
    """

    def __init__(self):
        self._entries: dict[str, PoolEntry] = {}   # alias -> entry
        self._lock = threading.Lock()

    # ── 注册/注销 ──────────────────────────────────────

    def register(
        self,
        model_id: str,
        alias: str = "",
        weight: float = 1.0,
        api_key: str = "",
        base_url: str = "",
        **overrides: Any,
    ) -> PoolEntry:
        """注册或更新一个模型。"""
        alias = alias or model_id.split("/")[-1].replace(".", "-")
        capability = _infer_capability(model_id)
        # overrides
        for k, v in overrides.items():
            if hasattr(capability, k):
                setattr(capability, k, v)

        entry = PoolEntry(
            model_id=model_id, alias=alias, weight=weight,
            capability=capability, api_key=api_key, base_url=base_url,
        )
        with self._lock:
            self._entries[alias] = entry
        return entry

    def unregister(self, alias: str) -> bool:
        """移除模型。"""
        with self._lock:
            if alias in self._entries:
                del self._entries[alias]
                return True
        return False

    def get(self, alias: str) -> PoolEntry | None:
        with self._lock:
            return self._entries.get(alias)

    def list_all(self) -> list[PoolEntry]:
        with self._lock:
            return list(self._entries.values())

    # ── 健康更新 ───────────────────────────────────────

    def record_success(self, alias: str, latency: float = 0.0) -> None:
        """记录一次成功调用."""
        with self._lock:
            entry = self._entries.get(alias)
            if not entry:
                return
            h = entry.health
            h.total_calls += 1
            h.success_count += 1
            h.consecutive_failures = 0
            h.circuit_open_until = 0.0
            h.last_used_at = time.monotonic()
            if latency > 0:
                h.last_latencies.append(latency)
                if len(h.last_latencies) > 10:
                    h.last_latencies.pop(0)
                h.avg_latency = sum(h.last_latencies) / len(h.last_latencies)

    def record_failure(self, alias: str) -> None:
        """记录一次失败调用."""
        with self._lock:
            entry = self._entries.get(alias)
            if not entry:
                return
            h = entry.health
            h.total_calls += 1
            h.failure_count += 1
            h.consecutive_failures += 1
            h.last_used_at = time.monotonic()
            if h.consecutive_failures >= FAILURE_THRESHOLD:
                cooldown = min(
                    COOLDOWN_BASE * (h.consecutive_failures - FAILURE_THRESHOLD + 1),
                    MAX_COOLDOWN,
                )
                h.circuit_open_until = time.monotonic() + cooldown

    # ── 选择 ───────────────────────────────────────────

    def get_healthy(self) -> list[PoolEntry]:
        """排除断路器打开的模型."""
        now = time.monotonic()
        with self._lock:
            return [
                e for e in self._entries.values()
                if e.health.circuit_open_until <= now
            ]

    def select_best(
        self, profile: Any, count: int = 3,
    ) -> list[PoolEntry]:
        """根据任务 profile 选择最佳模型。

        Args:
            profile: TaskProfile (from difficulty_estimator)
            count: 返回前 N 个模型（用于 fallback）

        Returns:
            按分数降序排列的模型列表。
        """
        healthy = self.get_healthy()
        if not healthy:
            # fallback: all models regardless of health
            with self._lock:
                healthy = list(self._entries.values())
            if not healthy:
                return []

        scored = [(self._score(e, profile), e) for e in healthy]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:count]]

    def _score(self, entry: PoolEntry, profile: Any) -> float:
        """为 (模型, 任务) 打分，越高越好."""
        cap = entry.capability
        h = entry.health
        score = entry.weight * 2.0

        # Capability match
        if getattr(profile, "requires_reasoning", False):
            score += cap.reasoning_score * 4.0
        if getattr(profile, "requires_code_generation", False):
            score += cap.coding_score * 3.0
        if getattr(profile, "requires_tools", False):
            score += cap.tool_use_score * 2.0
            if not cap.supports_tools:
                score -= 3.0

        # Cost optimization
        complexity = getattr(profile, "complexity", 0.5)
        if complexity < 0.3:
            score += cap.cost_efficiency * 3.0
        elif complexity > 0.7:
            score += cap.tier * 1.0

        # Context window
        est_tokens = getattr(profile, "estimated_tokens", 0)
        if est_tokens > cap.context_window * 0.85:
            score -= 8.0

        # Health
        if h.consecutive_failures > 0:
            score -= float(h.consecutive_failures) * 2.0

        return max(score, 0.0)

    # ── 序列化 ─────────────────────────────────────────

    def to_config(self) -> dict[str, dict[str, Any]]:
        """导出配置（用于持久化）."""
        with self._lock:
            return {
                alias: {
                    "model_id": e.model_id,
                    "weight": e.weight,
                    "api_key": e.api_key,
                    "base_url": e.base_url,
                }
                for alias, e in self._entries.items()
            }

    def from_config(self, data: dict[str, dict[str, Any]]) -> None:
        """从配置恢复."""
        with self._lock:
            self._entries.clear()
            for alias, cfg in data.items():
                self.register(
                    model_id=cfg.get("model_id", ""),
                    alias=alias,
                    weight=cfg.get("weight", 1.0),
                    api_key=cfg.get("api_key", ""),
                    base_url=cfg.get("base_url", ""),
                )


def _infer_capability(model_id: str) -> CapabilityProfile:
    """从模型名推断能力画像."""
    parts = model_id.split("/", 1)
    provider = parts[0] if len(parts) > 1 else ""
    name = (parts[1] if len(parts) > 1 else model_id).lower()

    # Tier
    flagship_kw = ("pro", "max", "plus", "large", "opus", "sonnet-4",
                   "v4-pro", "4o", "4.0", "turbo-2025")
    budget_kw = ("mini", "flash", "lite", "small", "medium", "v4-flash",
                 "1.5-flash", "haiku", "air", "8k")

    if any(kw in name for kw in flagship_kw):
        tier = 5
    elif any(kw in name for kw in budget_kw):
        tier = 2
    else:
        tier = 3

    # Reasoning
    reasoning_kw = ("reasoner", "o1", "o3", "opus", "thinking", "sonnet")
    reasoning_score = 0.8 if any(kw in name for kw in reasoning_kw) else (
        0.5 if tier >= 4 else 0.3
    )

    # Coding
    coding_kw = ("coder", "sonnet", "v4-pro", "deepseek")
    coding_score = 0.85 if any(kw in name for kw in coding_kw) else (
        0.6 if tier >= 4 else 0.3
    )

    # Tool use
    tool_score = 0.8 if tier >= 4 else 0.5
    if provider == "anthropic":
        tool_score = 0.9

    # Cost
    cost_map = {
        "openai": 0.3, "anthropic": 0.2, "deepseek": 0.8,
        "google": 0.6, "zhipu": 0.7, "qwen": 0.7,
        "moonshot": 0.6, "ollama": 0.9, "xiaomi": 0.6,
        "baichuan": 0.7, "minimax": 0.6,
    }
    base_cost = cost_map.get(provider, 0.5)
    cost_efficiency = max(0.1, base_cost - (tier - 1) * 0.1)

    # Context window
    ctx = 128000
    for kw, val in (("128k", 128000), ("32k", 32000), ("16k", 16000)):
        if kw in name:
            ctx = val
            break
    if provider == "anthropic":
        ctx = max(ctx, 200000)
    elif provider == "google":
        ctx = max(ctx, 1000000)

    return CapabilityProfile(
        tier=tier,
        reasoning_score=reasoning_score,
        coding_score=coding_score,
        tool_use_score=tool_score,
        cost_efficiency=cost_efficiency,
        context_window=ctx,
        supports_tools=tier >= 2,
        supports_streaming=True,
    )
