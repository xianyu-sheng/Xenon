"""BudgetManager — ReAct 迭代预算管理（F2 / §Q2）。

三阶段软预算模型，对应规范 Q2 的"探索 25% / 执行 / 收束"叙事：

- **EXPLORE**（前 25%）：鼓励探索，信息型工具不受限；
- **EXECUTE**（中段 50%）：正常执行；
- **CONVERGE**（末 25%）：收束阶段，禁用纯探索型工具（list_files/search_files/
  code_index/ast_analyze/diff_preview/web_fetch/github_fetch），强制 LLM 走向
  合成与 final_answer。``read_file`` 不在禁用之列——收束阶段仍允许最终验证。

奖励机制（规范 Q2 核心，"软预算"）：良好行为换额外轮次——

- ``on_compression()``：上下文被压缩（省 token）→ +N 轮；
- ``on_hollow_answer()``：检测到空洞回答 → +N 轮（给机会补救，而非直接判失败）。

``bonus`` 只扩展 ``can_continue()`` 上限，**不改变阶段边界**（CONVERGE 仍按 base
的 75% 触发），保证收束节奏不被奖励打乱。``max_total_multiplier`` 给 bonus 封顶
（默认 2× base），防止空洞奖励无限累积导致运行失控。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class BudgetPhase(str, Enum):
    """三阶段。str 子类便于序列化与日志可读。"""

    EXPLORE = "explore"
    EXECUTE = "execute"
    CONVERGE = "converge"


# 收束阶段禁用的纯探索型工具（只读、易沦为拖延）。
# read_file 显式豁免——收束阶段仍允许"读一次验证"，只拦"反复 list/search 探索"。
CONVERGE_BLOCKED_TOOLS = frozenset({
    "list_files", "search_files", "code_index", "ast_analyze",
    "diff_preview", "web_fetch", "github_fetch",
})


@dataclass
class BudgetManager:
    """ReAct 迭代预算管理器。

    用法::

        budget = BudgetManager(max_iterations=10)
        while budget.can_continue():
            budget.spend()
            ...
            if compressed:
                budget.on_compression()
            if hollow_detector.is_hollow(answer):
                budget.on_hollow_answer()
            allow, reason = budget.allow_tool(action)
            if not allow:
                # 注入 reason 作为 observation，不执行
    """

    max_iterations: int
    spent: int = 0
    bonus: int = 0
    # 奖励配额（默认值，可覆盖）
    compression_reward: int = 2
    hollow_reward: int = 3
    # 阶段边界（占 base max 的比例）
    explore_ratio: float = 0.25
    converge_ratio: float = 0.75
    # bonus 封顶倍数（默认 2× base，防止奖励无限累积）
    max_total_multiplier: float = 2.0
    # 收束阶段禁用工具集（可注入便于测试/扩展）
    blocked_in_converge: frozenset[str] = field(default_factory=lambda: CONVERGE_BLOCKED_TOOLS)
    # 奖励历史（可观测，供 mercy compile / debug 用）
    rewards: list[tuple[str, int]] = field(default_factory=list)

    # ── 基础查询 ──────────────────────────────────────────────

    @property
    def total(self) -> int:
        """有效预算上限 = base + bonus，受 ``max_total_multiplier`` 封顶。"""
        cap = int(self.max_iterations * self.max_total_multiplier)
        return min(self.max_iterations + self.bonus, cap)

    @property
    def remaining(self) -> int:
        """剩余可用轮次。"""
        return max(0, self.total - self.spent)

    @property
    def ratio(self) -> float:
        """已耗占 **base** 的比例（阶段判定基准，不受 bonus 影响）。"""
        if self.max_iterations <= 0:
            return 1.0
        return self.spent / self.max_iterations

    @property
    def phase(self) -> BudgetPhase:
        """当前阶段（基于 base 比例，bonus 不改变边界）。"""
        if self.ratio < self.explore_ratio:
            return BudgetPhase.EXPLORE
        if self.ratio >= self.converge_ratio:
            return BudgetPhase.CONVERGE
        return BudgetPhase.EXECUTE

    def is_explore_phase(self) -> bool:
        return self.phase is BudgetPhase.EXPLORE

    def is_execute_phase(self) -> bool:
        return self.phase is BudgetPhase.EXECUTE

    def is_converge_phase(self) -> bool:
        return self.phase is BudgetPhase.CONVERGE

    def can_continue(self) -> bool:
        """是否还有预算继续迭代。"""
        return self.spent < self.total

    # ── 状态推进 ──────────────────────────────────────────────

    def spend(self, n: int = 1) -> int:
        """消耗 n 轮，返回消耗后的 spent。"""
        if n > 0:
            self.spent += n
        return self.spent

    def on_compression(self, n: int | None = None) -> int:
        """上下文压缩奖励：+N 轮（默认 ``compression_reward``）。

        压缩省了 token，奖励额外轮次让 agent 把省下的预算用在执行上。
        """
        reward = self.compression_reward if n is None else max(0, n)
        return self._grant("compression", reward)

    def on_hollow_answer(self, n: int | None = None) -> int:
        """空洞回答奖励：+N 轮给机会补救（默认 ``hollow_reward``）。

        检测到空洞回答时，软预算给额外轮次让 LLM 重试产出具体内容，
        而非立即判失败/截断——契合规范 Q2 的"奖励良好行为"哲学
        （补救本身是良好行为，值得给资源）。
        """
        reward = self.hollow_reward if n is None else max(0, n)
        return self._grant("hollow", reward)

    def _grant(self, kind: str, reward: int) -> int:
        """发放奖励，受 ``max_total_multiplier`` 封顶；超封顶则记 0。"""
        if reward <= 0:
            return self.bonus
        cap = int(self.max_iterations * self.max_total_multiplier)
        # 已达封顶：不再发放，但记录尝试（可观测）
        if self.max_iterations + self.bonus >= cap:
            self.rewards.append((kind, 0))
            logger.info(
                f"BudgetManager: {kind} 奖励被封顶拒绝（已达 {self.total}/{cap}）"
            )
            return self.bonus
        # 部分发放：不超过封顶
        granted = min(reward, cap - (self.max_iterations + self.bonus))
        self.bonus += granted
        self.rewards.append((kind, granted))
        logger.info(
            f"BudgetManager: {kind} 奖励 +{granted}（请求 {reward}），"
            f"有效预算 {self.total}/{cap}"
        )
        return self.bonus

    # ── 工具门控 ──────────────────────────────────────────────

    def allow_tool(self, tool_name: str) -> tuple[bool, str]:
        """收束阶段禁用纯探索型工具；其余放行。

        返回 ``(allow, reason)``。被禁用时 ``reason`` 供引擎注入 observation 提示。
        """
        if self.is_converge_phase() and tool_name in self.blocked_in_converge:
            return (
                False,
                f"收束阶段禁用探索型工具 {tool_name}，请直接合成最终答案"
                "（如需验证用 read_file，不要再 list/search 探索）",
            )
        return True, ""

    # ── 可观测 ────────────────────────────────────────────────

    def summary(self) -> str:
        """人类可读预算摘要，供日志/mercy compile 注入。"""
        return (
            f"预算 {self.spent}/{self.total}（base {self.max_iterations}"
            f"+奖励 {self.bonus}），阶段={self.phase.value}，剩余 {self.remaining}"
        )

    def reset(self) -> None:
        """清空状态（复用实例时调用）。"""
        self.spent = 0
        self.bonus = 0
        self.rewards.clear()
