"""
v0.4.0: 加权模型池 + 健康追踪 + 断路器 + 多优先级队列调度。

替代 role_priority 列表，提供：
- CapabilityProfile: 从模型名推断能力画像
- HealthRecord: 运行时成功率、延迟、断路器状态
- ModelPool: 加权选择 + 健康感知路由 + 5 级优先级队列 + 工作窃取

v0.4.0 Step 10: 新增 _tier_queues 多优先级队列调度，OS 进程调度风格。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

MIN_TIER = 1
MAX_TIER = 5


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
    """加权模型池 + 健康追踪 + 断路器 + 多优先级队列调度。

    线程安全，支持并发更新。

    v0.4.0 Step 10: 模型按 CapabilityProfile.tier 组织到 5 级队列，
    select_best 时先确定任务 tier，优先从匹配队列选模型，
    空则向更高/更低 tier 窃取。
    """

    def __init__(self):
        self._entries: dict[str, PoolEntry] = {}   # alias -> entry
        self._tier_queues: dict[int, list[str]] = {  # tier -> [alias, ...]
            t: [] for t in range(MIN_TIER, MAX_TIER + 1)
        }
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

        # Step 11: 尝试从基准测试获取 tier（惰性导入，失败静默回退）
        if "tier" not in overrides:
            try:
                from omniagent.repl.benchmark_fetcher import get_benchmark_fetcher
                fetcher = get_benchmark_fetcher()
                benchmark_tier = fetcher.estimate_tier(model_id, fallback_tier=capability.tier)
                if benchmark_tier != capability.tier:
                    capability.tier = benchmark_tier
            except Exception:
                pass  # 基准获取失败，使用 _infer_capability 的 tier

        # overrides（在基准测试之后应用，确保显式覆盖总是胜出）
        for k, v in overrides.items():
            if hasattr(capability, k):
                setattr(capability, k, v)

        entry = PoolEntry(
            model_id=model_id, alias=alias, weight=weight,
            capability=capability, api_key=api_key, base_url=base_url,
        )
        with self._lock:
            self._entries[alias] = entry
            tier = min(max(entry.capability.tier, MIN_TIER), MAX_TIER)
            if alias not in self._tier_queues[tier]:
                self._tier_queues[tier].append(alias)
        return entry

    def unregister(self, alias: str) -> bool:
        """移除模型。"""
        with self._lock:
            if alias not in self._entries:
                return False
            del self._entries[alias]
            for t in range(MIN_TIER, MAX_TIER + 1):
                if alias in self._tier_queues[t]:
                    self._tier_queues[t].remove(alias)
            return True

    def get(self, alias: str) -> PoolEntry | None:
        with self._lock:
            return self._entries.get(alias)

    def list_all(self) -> list[PoolEntry]:
        with self._lock:
            return list(self._entries.values())

    def is_empty(self) -> bool:
        with self._lock:
            return len(self._entries) == 0

    def get_tier_queues(self) -> dict[int, list[str]]:
        """返回 tier 队列的快照（供 /pool 展示）。"""
        with self._lock:
            return {t: list(aliases) for t, aliases in self._tier_queues.items()}

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

    # ── 多优先级队列调度 ───────────────────────────────

    def _resolve_tier_queue(self, tier: int) -> list[str]:
        """为给定 tier 解析可用的模型别名，需要时进行工作窃取。

        策略（OS 调度启发式）：
        1. 先从目标 tier 找健康模型
        2. 空则向更高 tier 窃取（tier+1 → 5，优先更好模型）
        3. 仍空则向更低 tier 窃取（tier-1 → 1）
        4. 全部空返回空列表
        """
        now = time.monotonic()

        def _healthy_aliases(t: int) -> list[str]:
            return [
                a for a in self._tier_queues.get(t, [])
                if a in self._entries
                and self._entries[a].health.circuit_open_until <= now
            ]

        # 1. 目标 tier
        candidates = _healthy_aliases(tier)
        if candidates:
            return candidates

        # 2. 向更高 tier 窃取
        for t in range(tier + 1, MAX_TIER + 1):
            candidates = _healthy_aliases(t)
            if candidates:
                return candidates

        # 3. 向更低 tier 窃取
        for t in range(tier - 1, MIN_TIER - 1, -1):
            candidates = _healthy_aliases(t)
            if candidates:
                return candidates

        return []

    @staticmethod
    def _resolve_task_tier(profile: Any) -> int:
        """从 TaskProfile 推断任务所需的模型 tier。"""
        # 优先读取 DifficultyEstimator.estimate_tier 设置的值
        tier = getattr(profile, "_tier", None)
        if tier is not None and MIN_TIER <= tier <= MAX_TIER:
            return tier
        # 回退：从 complexity 计算
        complexity = getattr(profile, "complexity", 0.3)
        normalized = min(max(complexity, 0.0), 1.0)
        return min(MAX_TIER, max(MIN_TIER, int(normalized * MAX_TIER) + 1))

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

        v0.4.0 Step 10: 先确定任务 tier，从匹配队列中选模型，
        用 _score 细粒度排序。队列空时 fallback 到全局。

        Args:
            profile: TaskProfile (from difficulty_estimator)
            count: 返回前 N 个模型（用于 fallback）

        Returns:
            按分数降序排列的模型列表。
        """
        # Step 10: 多优先级队列调度
        task_tier = self._resolve_task_tier(profile)
        aliases = self._resolve_tier_queue(task_tier)

        if aliases:
            entries = []
            with self._lock:
                for a in aliases:
                    if e := self._entries.get(a):
                        entries.append(e)
            if entries:
                scored = [(self._score(e, profile), e) for e in entries]
                scored.sort(key=lambda x: x[0], reverse=True)
                return [e for _, e in scored[:count]]

        # Fallback: 全局搜索（所有健康模型）
        healthy = self.get_healthy()
        if not healthy:
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

    def score_for_profile(self, entry: PoolEntry, profile: Any) -> float:
        """公开的打分方法，供 history 记录使用。"""
        return self._score(entry, profile)

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
        """从配置恢复（会重建 tier 队列）."""
        # 先清空（释放锁后再逐条 register，避免 register 内部加锁时死锁）
        with self._lock:
            self._entries.clear()
            for t in range(MIN_TIER, MAX_TIER + 1):
                self._tier_queues[t].clear()
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
