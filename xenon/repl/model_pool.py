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
    """运行时健康指标（动态，每次 API 调用更新）。

    v0.7.0: 阈值熔断 + 有上限的指数退避机制。
    - consecutive_failures: 连续失败次数（断路器用）
    - retry_cycle_count: 退避周期计数，每完成一次"禁止→解禁→重试仍失败"的周期 +1
    - circuit_open_until: 断路器打开截止时间戳，0 = 关闭
    - permanently_evicted: 仅供用户显式的运行时驱逐；网络失败不会修改配置
    """
    total_calls: int = 0
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    retry_cycle_count: int = 0
    avg_latency: float = 0.0
    last_latencies: list[float] = field(default_factory=list)
    circuit_open_until: float = 0.0          # timestamp, 0 = closed
    last_used_at: float = 0.0
    permanently_evicted: bool = False
    in_flight: int = 0          # P2: 当前并发调用数(资源感知调度用)


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


FAILURE_THRESHOLD = 3        # 连续失败次数触发断路器
COOLDOWN_BASE = 30.0         # 首次退避时间（秒）
MAX_COOLDOWN = 600.0         # 最大退避时间（秒）
MAX_RETRY_CYCLES = 3         # 达到后保持 MAX_COOLDOWN，但不删除用户配置


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
        self.perf_profile: str = "balanced"  # P2: fast|cost|balanced -> _score 权重向量

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
                from xenon.repl.benchmark_fetcher import get_benchmark_fetcher
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

    def evict_permanently(self, alias: str) -> bool:
        """Explicitly evict a model from this in-memory pool only.

        User credentials are configuration, not runtime health state, and are
        never deleted by a circuit breaker.
        """
        with self._lock:
            entry = self._find_entry(alias)
            if not entry:
                return False
            entry.health.permanently_evicted = True
            entry.health.circuit_open_until = float("inf")
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

    # ── 查找辅助 ───────────────────────────────────────

    def _find_entry(self, alias_or_model_id: str) -> PoolEntry | None:
        """按 alias 或 model_id 查找条目。"""
        entry = self._entries.get(alias_or_model_id)
        if entry is not None:
            return entry
        # Fallback: 按 model_id 查找（engine 层传的是完整 model_id）
        for e in self._entries.values():
            if e.model_id == alias_or_model_id:
                return e
        return None

    # ── P2: 资源感知(并发计数 + 性能偏好)──────────────

    def acquire(self, model_id: str) -> None:
        """调用前并发计数 +1(供 _score 负载因子)。线程安全。"""
        with self._lock:
            entry = self._find_entry(model_id)
            if entry:
                entry.health.in_flight += 1

    def release(self, model_id: str) -> None:
        """调用后并发计数 -1(无论成败)。线程安全,不低于 0。"""
        with self._lock:
            entry = self._find_entry(model_id)
            if entry and entry.health.in_flight > 0:
                entry.health.in_flight -= 1

    def set_perf_profile(self, profile: str) -> bool:
        """设置性能偏好(fast|cost|balanced),影响 _score 权重向量。"""
        if profile in ("fast", "cost", "balanced"):
            self.perf_profile = profile
            return True
        return False

    # ── 健康更新 ───────────────────────────────────────

    def record_success(self, alias: str, latency: float = 0.0) -> None:
        """记录一次成功调用。alias 可以是 alias 或完整 model_id。

        v0.5.3: 成功后重置退避周期计数和断路器。
        """
        with self._lock:
            entry = self._find_entry(alias)
            if not entry:
                return
            h = entry.health
            h.total_calls += 1
            h.success_count += 1
            h.consecutive_failures = 0
            h.retry_cycle_count = 0       # v0.5.3: 成功后重置退避计数
            h.circuit_open_until = 0.0
            h.permanently_evicted = False
            h.last_used_at = time.monotonic()
            if latency > 0:
                h.last_latencies.append(latency)
                if len(h.last_latencies) > 10:
                    h.last_latencies.pop(0)
                h.avg_latency = sum(h.last_latencies) / len(h.last_latencies)

    def record_failure(self, alias: str, *, is_retry: bool = False) -> bool:
        """记录一次失败调用。alias 可以是 alias 或完整 model_id。

        v0.7.0: 阈值熔断 + 有上限的指数退避。
        - 前 ``FAILURE_THRESHOLD - 1`` 次失败只降低健康分，不熔断
        - 达到阈值后断路器打开 ``COOLDOWN_BASE`` 秒
        - 断路器到期后重试仍失败 (is_retry=True): 退避周期 +1，退避时间翻倍
        - 达到 MAX_RETRY_CYCLES 后只使用 MAX_COOLDOWN，不删除模型或凭据

        Args:
            alias: 模型别名或完整 model_id
            is_retry: True 表示这是一次解禁后的重试（退避周期 +1）

        Returns:
            保留兼容返回值；运行时失败不再永久驱逐，始终为 False。
        """
        with self._lock:
            entry = self._find_entry(alias)
            if not entry:
                return False
            h = entry.health
            h.total_calls += 1
            h.failure_count += 1
            h.consecutive_failures += 1
            h.last_used_at = time.monotonic()

            if h.consecutive_failures < FAILURE_THRESHOLD:
                h.circuit_open_until = 0.0
                return False

            if is_retry:
                # 解禁后重试仍然失败 → 退避周期 +1，退避时间指数增长
                h.retry_cycle_count += 1

            # 指数退避: COOLDOWN_BASE * 2^(retry_cycle_count)
            h.retry_cycle_count = min(h.retry_cycle_count, MAX_RETRY_CYCLES)
            multiplier = 2 ** h.retry_cycle_count
            cooldown = min(COOLDOWN_BASE * multiplier, MAX_COOLDOWN)
            h.circuit_open_until = time.monotonic() + cooldown
            return False

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
                and not self._entries[a].health.permanently_evicted
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
        """排除断路器打开或已永久驱逐的模型."""
        now = time.monotonic()
        with self._lock:
            return [
                e for e in self._entries.values()
                if e.health.circuit_open_until <= now
                and not e.health.permanently_evicted
            ]

    def select_best(
        self, profile: Any, count: int = 3,
    ) -> list[PoolEntry]:
        """根据任务 profile 选择最佳模型。

        v0.4.0 Step 10: 先确定任务 tier，从匹配队列中选模型，
        用 _score 细粒度排序。队列空时 fallback 到全局。
        v0.5.6: Tier 边界模糊，也考虑相邻 tier 的模型。

        Args:
            profile: TaskProfile (from difficulty_estimator)
            count: 返回前 N 个模型（用于 fallback）

        Returns:
            按分数降序排列的模型列表。
        """
        # Step 10: 多优先级队列调度
        task_tier = self._resolve_task_tier(profile)

        # v0.5.6: Tier 边界模糊 — 同时考虑目标 tier 和相邻 tier
        tiers_to_check = [task_tier]
        if task_tier > MIN_TIER:
            tiers_to_check.append(task_tier - 1)
        if task_tier < MAX_TIER:
            tiers_to_check.append(task_tier + 1)

        # 收集所有候选
        seen_aliases = set()
        entries = []
        with self._lock:
            for t in tiers_to_check:
                aliases = self._resolve_tier_queue(t)
                for a in aliases:
                    if a not in seen_aliases and (e := self._entries.get(a)):
                        seen_aliases.add(a)
                        entries.append(e)

        if entries:
            scored = [(self._score(e, profile), e) for e in entries]
            scored.sort(key=lambda x: x[0], reverse=True)
            return [e for _, e in scored[:count]]

        # Fallback: 全局搜索（所有健康模型）
        healthy = self.get_healthy()
        if not healthy:
            return []

        scored = [(self._score(e, profile), e) for e in healthy]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:count]]

    def _score(self, entry: PoolEntry, profile: Any) -> float:
        """为 (模型, 任务) 打分，越高越好.

        P2: 权重向量受 perf_profile(fast|cost|balanced)动态调整;
        新增 in_flight 负载因子(并发高的扣分)。
        v0.5.6: 根据 intent 调整权重，让任务类型真正影响路由。
        """
        cap = entry.capability
        h = entry.health
        score = entry.weight * 2.0

        # P2: 性能偏好 -> 能力匹配权重向量
        #   balanced: quality 主导(reasoning×4/coding×3/tools×2,与历史行为一致)
        #   fast    : 降低 quality 权重,偏好低 tier(轻量极速)+ 延迟惩罚
        #   cost    : quality 权重略降,成本权重显著提升
        if self.perf_profile == "fast":
            w_reason, w_coding, w_tools = 2.0, 1.5, 1.5
            score -= float(cap.tier) * 0.5
        elif self.perf_profile == "cost":
            w_reason, w_coding, w_tools = 3.0, 2.0, 1.5
        else:
            w_reason, w_coding, w_tools = 4.0, 3.0, 2.0

        # v0.5.6: Intent-specific weighting（真正按任务类型路由）
        intent = getattr(profile, "intent", None)
        if intent == "novel":
            # 小说：创意推理 + 长上下文最重要
            w_reason *= 1.5
            score += float(cap.context_window) * 0.00001
        elif intent == "debug":
            # 调试：推理能力 + 工具使用都重要
            w_reason *= 1.3
            w_tools *= 1.2
        elif intent == "design":
            # 设计：推理能力最重要
            w_reason *= 1.4
        elif intent == "refactor":
            # 重构：推理 + 代码能力
            w_reason *= 1.2
            w_coding *= 1.2
        elif intent == "write_test":
            # 写测试：代码能力 + 工具使用
            w_coding *= 1.3
            w_tools *= 1.2
        elif intent in ("chat", "query", "explain"):
            # 简单对话/查询：工具使用更重要（快速响应）
            w_tools *= 1.2

        # Capability match
        if getattr(profile, "requires_reasoning", False):
            score += cap.reasoning_score * w_reason
        if getattr(profile, "requires_code_generation", False):
            score += cap.coding_score * w_coding
        if getattr(profile, "requires_tools", False):
            score += cap.tool_use_score * w_tools
            if not cap.supports_tools:
                score -= 3.0

        # Cost optimization
        complexity = getattr(profile, "complexity", 0.5)
        if self.perf_profile == "cost":
            # cost 偏好:任何复杂度都纳入成本权重
            score += cap.cost_efficiency * 4.0
        elif complexity < 0.3:
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

        # P2: 负载因子(并发高的模型扣分,避免过载;fast 偏好更敏感)
        if h.in_flight > 0:
            load_penalty = 1.5 if self.perf_profile == "fast" else 1.0
            score -= float(h.in_flight) * load_penalty

        # P2: fast 偏好纳入延迟惩罚(历史平均延迟高的扣分)
        if self.perf_profile == "fast" and h.avg_latency > 0:
            score -= min(h.avg_latency * 0.5, 3.0)

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
