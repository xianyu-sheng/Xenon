"""
v0.5.0: 分层上下文压缩策略。

根据任务 tier（Q1-Q5）动态选择压缩策略，包括：
- 压缩触发阈值、工具输出保留长度、摘要段数、衰减率
- 紧急空间下的分层截断
- BudgetManager 阶段感知的工具输出处理
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xenon.engine.budget import BudgetPhase


# ── 工具输出类型 ────────────────────────────────────────────


class ToolOutputType(str, Enum):
    """工具输出分类（v0.5.0）。"""
    STRUCTURAL = "structural"   # read_file, ast_analyze — 保留签名，截断函数体
    TRANSIENT = "transient"     # command, web_fetch — 保留退出码 + 头尾
    POINTER = "pointer"         # search_files, list_files — 保留为路径列表
    MUTATION = "mutation"       # write_file, edit_file — 保留 diff 摘要
    META = "meta"               # git, mcp_call — 保留操作名 + 结果摘要


# ── 压缩策略 ────────────────────────────────────────────────


@dataclass
class CompressionStrategy:
    """单个 tier 的压缩策略参数集。"""

    tier: int
    trigger_threshold: float       # 触发压缩的 usage_ratio（0-1）
    tool_output_max_chars: int     # 工具输出保留最大字符数
    summary_segments: int          # 摘要段数（3 或 6）
    preserve_reasoning: bool       # 是否保留推理链
    decay_rate: float              # 每轮重要性衰减率
    keep_recent_rounds: int        # 保留最近 N 轮完整对话
    # 危急模式策略
    crisis_action: str = "auto_summary"  # drop | label | auto_summary | structured_truncate | cross_tier_evict
    crisis_truncate_head: int = 150      # 结构化截断保留前 N 字符
    crisis_truncate_tail: int = 100      # 结构化截断保留后 N 字符
    crisis_keep_top_n: int = 5           # 结构化截断保留最重要 N 条
    working_memory_keys: list[str] = field(default_factory=list)

    def apply_phase_modifier(self, phase: str) -> CompressionStrategy:
        """根据 BudgetManager 阶段调整策略参数，返回新策略（不修改原实例）。"""
        import copy
        modified = copy.copy(self)
        if phase == "explore":
            modified.tool_output_max_chars = self.tool_output_max_chars * 4
        elif phase == "converge":
            modified.tool_output_max_chars = max(50, int(self.tool_output_max_chars * 0.3))
            modified.trigger_threshold = max(0.35, self.trigger_threshold - 0.15)
        return modified


# ── 五级策略预设 ────────────────────────────────────────────


_PRESETS: dict[int, CompressionStrategy] = {
    1: CompressionStrategy(
        tier=1,
        trigger_threshold=0.50,
        tool_output_max_chars=100,
        summary_segments=3,
        preserve_reasoning=False,
        decay_rate=0.75,
        keep_recent_rounds=1,
        crisis_action="drop",
    ),
    2: CompressionStrategy(
        tier=2,
        trigger_threshold=0.55,
        tool_output_max_chars=300,
        summary_segments=3,
        preserve_reasoning=False,
        decay_rate=0.85,
        keep_recent_rounds=2,
        crisis_action="label",
    ),
    3: CompressionStrategy(
        tier=3,
        trigger_threshold=0.60,
        tool_output_max_chars=500,
        summary_segments=6,
        preserve_reasoning=False,
        decay_rate=0.90,
        keep_recent_rounds=3,
        crisis_action="auto_summary",
        working_memory_keys=["file_state"],
    ),
    4: CompressionStrategy(
        tier=4,
        trigger_threshold=0.75,
        tool_output_max_chars=1000,
        summary_segments=6,
        preserve_reasoning=True,
        decay_rate=0.93,
        keep_recent_rounds=4,
        crisis_action="structured_truncate",
        crisis_truncate_head=150,
        crisis_truncate_tail=100,
        crisis_keep_top_n=5,
        working_memory_keys=["file_state", "constraints"],
    ),
    5: CompressionStrategy(
        tier=5,
        trigger_threshold=0.85,
        tool_output_max_chars=2000,
        summary_segments=6,
        preserve_reasoning=True,
        decay_rate=0.96,
        keep_recent_rounds=5,
        crisis_action="cross_tier_evict",
        crisis_truncate_head=300,
        crisis_truncate_tail=200,
        crisis_keep_top_n=10,
        working_memory_keys=["file_state", "constraints", "architecture", "key_data"],
    ),
}


# ── 策略选择器 ──────────────────────────────────────────────


class TieredStrategySelector:
    """根据 task_tier 选择压缩策略。

    用法::

        selector = TieredStrategySelector()
        strategy = selector.select(tier=4, phase="execute")
    """

    def select(self, tier: int, phase: str | BudgetPhase | None = None) -> CompressionStrategy:
        """选择并返回（可选阶段调整后的）压缩策略。

        Args:
            tier: 任务层级 (1-5)，超出范围时 clamp。
            phase: BudgetManager 阶段 ("explore"|"execute"|"converge") 或 BudgetPhase 枚举。
        """
        tier = max(1, min(5, tier))
        strategy = _PRESETS[tier]
        if phase is not None:
            phase_str = phase.value if hasattr(phase, "value") else str(phase)
            strategy = strategy.apply_phase_modifier(phase_str)
        return strategy

    @staticmethod
    def get_preset(tier: int) -> CompressionStrategy:
        """获取未经阶段调整的原始预设策略。"""
        tier = max(1, min(5, tier))
        return _PRESETS[tier]


# ── 工具输出分类器 ──────────────────────────────────────────


# 工具名 → 类型映射
_TOOL_TYPE_MAP: dict[str, ToolOutputType] = {
    # 结构化 — 保留签名，截断函数体
    "read_file": ToolOutputType.STRUCTURAL,
    "code_index": ToolOutputType.STRUCTURAL,
    "ast_analyze": ToolOutputType.STRUCTURAL,
    # 瞬时型 — 保留退出码 + 头尾
    "command": ToolOutputType.TRANSIENT,
    "web_fetch": ToolOutputType.TRANSIENT,
    "github_fetch": ToolOutputType.TRANSIENT,
    # 指针型 — 保留为路径列表
    "search_files": ToolOutputType.POINTER,
    "list_files": ToolOutputType.POINTER,
    # 变更型 — 保留 diff 摘要
    "write_file": ToolOutputType.MUTATION,
    "edit_file": ToolOutputType.MUTATION,
    "batch_write": ToolOutputType.MUTATION,
    "batch_edit": ToolOutputType.MUTATION,
    "edit_with_llm": ToolOutputType.MUTATION,
    # 元操作型 — 保留操作名 + 结果摘要
    "git": ToolOutputType.META,
    "mcp_call": ToolOutputType.META,
    "register_tool": ToolOutputType.META,
    "diff_preview": ToolOutputType.META,
    "datetime": ToolOutputType.META,
}


class ToolOutputClassifier:
    """工具输出分类与压缩（v0.5.0）。

    用法::

        classifier = ToolOutputClassifier()
        compressed = classifier.compress("read_file", output, max_chars=500)
    """

    @staticmethod
    def classify(tool_name: str) -> ToolOutputType:
        """返回工具输出类型。"""
        return _TOOL_TYPE_MAP.get(tool_name, ToolOutputType.TRANSIENT)

    @staticmethod
    def compress(
        tool_name: str,
        output: str,
        max_chars: int = 500,
        phase: str | None = None,
    ) -> str:
        """根据工具类型和阶段压缩输出。

        Args:
            tool_name: 工具名称。
            output: 原始工具输出字符串。
            max_chars: 最大保留字符数（受阶段调整）。
            phase: BudgetManager 阶段。

        Returns:
            压缩后的输出字符串。
        """
        if not output:
            return output

        tool_type = ToolOutputClassifier.classify(tool_name)

        # 阶段调整 max_chars
        if phase == "explore":
            max_chars = max_chars * 4
        elif phase == "converge":
            max_chars = max(50, int(max_chars * 0.3))

        if tool_type == ToolOutputType.STRUCTURAL:
            return _compress_structural(output, max_chars)
        elif tool_type == ToolOutputType.TRANSIENT:
            return _compress_transient(output, max_chars)
        elif tool_type == ToolOutputType.POINTER:
            return _compress_pointer(output, max_chars)
        elif tool_type == ToolOutputType.MUTATION:
            return _compress_mutation(output, max_chars)
        elif tool_type == ToolOutputType.META:
            return _compress_meta(output, max_chars)
        else:
            return _compress_transient(output, max_chars)

    @staticmethod
    def compress_many(
        tool_outputs: list[tuple[str, str]],
        max_chars: int = 500,
        phase: str | None = None,
    ) -> list[str]:
        """批量压缩工具输出。"""
        return [
            ToolOutputClassifier.compress(name, out, max_chars, phase)
            for name, out in tool_outputs
        ]


def _compress_structural(output: str, max_chars: int) -> str:
    """压缩结构化输出（read_file 等）：保留 def/class 签名行，截断函数体。"""
    if len(output) <= max_chars:
        return output

    # 提取签名行
    sig_pattern = re.compile(
        r"^(\s*)(def\s+\w+\s*\(.*?\)|class\s+\w+\s*\(.*?\))\s*:",
        re.MULTILINE,
    )
    signatures = [m.group(0) for m in sig_pattern.finditer(output)]

    if signatures:
        head = output[: max_chars // 2]
        tail = output[-max_chars // 4 :]
        sig_lines = "\n".join(signatures[:20])
        return (
            f"{head}\n\n…（省略 {len(output) - len(head) - len(tail)} 字符）…\n\n"
            f"[结构摘要：{len(signatures)} 个函数/类]\n{sig_lines}\n\n…\n{tail}"
        )

    # 无签名 → 简单头尾截断
    head = output[: max_chars // 2]
    tail = output[-max_chars // 4 :]
    return f"{head}\n…（省略 {len(output) - len(head) - len(tail)} 字符）…\n{tail}"


def _compress_transient(output: str, max_chars: int) -> str:
    """压缩瞬时型输出（command, web_fetch）：保留退出码/状态 + 头尾。"""
    if len(output) <= max_chars:
        return output

    # 提取 exit code / status
    status = ""
    exit_match = re.search(r"(?:exit|return)\s*(?:code|status)?\s*[:=]?\s*(\d+)", output, re.I)
    if exit_match:
        status = f"[退出码: {exit_match.group(1)}] "

    head = output[: max_chars // 2]
    tail = output[-max_chars // 4 :]
    omitted = len(output) - len(head) - len(tail)
    return f"{status}{head}\n…（省略 {omitted} 字符）…\n{tail}"


def _compress_pointer(output: str, max_chars: int) -> str:
    """压缩指针型输出（search_files, list_files）：保留为路径列表。"""
    if len(output) <= max_chars:
        return output

    # 提取文件路径
    path_pattern = re.compile(
        r"(?:^|\s)((?:[\w\-./\\]+/)*[\w\-./\\]+\.\w{1,10})",
        re.MULTILINE,
    )
    paths = list(dict.fromkeys(path_pattern.findall(output)))[:50]  # 去重保序，最多 50 条

    if paths:
        return f"[{len(paths)} 个文件/目录]\n" + "\n".join(paths[: max_chars // 30])
    # 无路径 → 简单截断
    return output[:max_chars] + ("…" if len(output) > max_chars else "")


def _compress_mutation(output: str, max_chars: int) -> str:
    """压缩变更型输出（write_file, edit_file）：保留 diff 摘要。"""
    if len(output) <= max_chars:
        return output

    # 提取 diff 统计
    diff_lines = re.findall(r"^[+\-].*$", output, re.MULTILINE)
    added = sum(1 for l in diff_lines if l.startswith("+"))
    removed = sum(1 for l in diff_lines if l.startswith("-"))

    # 提取关键行
    head = output[: max_chars // 2]
    tail = output[-max_chars // 4 :]
    summary = f"[变更: +{added}/-{removed}]"
    return f"{summary}\n{head}\n…（省略）…\n{tail}"


def _compress_meta(output: str, max_chars: int) -> str:
    """压缩元操作型输出（git, mcp_call）：保留操作名 + 首行结果。"""
    if len(output) <= max_chars:
        return output

    first_line = output.split("\n", 1)[0][:200]
    return f"[元操作] {first_line}…（总 {len(output)} 字符）"


# ── 重要性衰减计算 ──────────────────────────────────────────


class ImportanceCalculator:
    """计算对话轮次的有效重要性（v0.5.0）。

    公式::

        effective = tier_score × decay_rate^(distance_from_current)

    其中 tier_score = task_tier / 5（归一化到 0.2-1.0）。
    """

    @staticmethod
    def tier_score(tier: int) -> float:
        """将 tier (1-5) 归一化为 0.2-1.0 的分数。"""
        return max(0.2, tier / 5.0)

    @staticmethod
    def effective_importance(
        turn_tier: int,
        turn_index: int,
        current_index: int,
        decay_rate: float,
    ) -> float:
        """计算单轮的有效重要性。

        Args:
            turn_tier: 该轮的任务层级 (1-5)。
            turn_index: 该轮的位置序号。
            current_index: 当前最新轮次序号。
            decay_rate: 衰减率（如 0.90 = 每轮衰减 10%）。

        Returns:
            0.0-1.0 的有效重要性分数。
        """
        distance = max(0, current_index - turn_index)
        base = ImportanceCalculator.tier_score(turn_tier)
        return base * (decay_rate ** distance)

    @staticmethod
    def filter_by_importance(
        turns: list,
        current_index: int,
        decay_rate: float,
        min_score: float = 0.1,
    ) -> list:
        """过滤掉重要性低于阈值的轮次。

        注意：user_input 类型的轮次始终保留。
        """
        result = []
        for turn in turns:
            turn_tier = getattr(turn, "task_tier", 3)
            turn_idx = getattr(turn, "turn_index", current_index)
            turn_type = getattr(turn, "turn_type", "general")
            score = ImportanceCalculator.effective_importance(
                turn_tier, turn_idx, current_index, decay_rate,
            )
            if score >= min_score or turn_type == "user_input":
                result.append(turn)
        return result


# ── 空间状态判定 ────────────────────────────────────────────


class SpaceBudget:
    """判定当前上下文是否有足够空间进行 LLM 压缩。

    用法::

        sb = SpaceBudget()
        state = sb.evaluate(usage_ratio=0.92)  # → "critical"
    """

    AMPLE_THRESHOLD = 0.15    # 空闲 >15% → 充裕
    TIGHT_THRESHOLD = 0.05    # 空闲 5-15% → 紧张，<5% → 危急

    @classmethod
    def evaluate(cls, usage_ratio: float) -> str:
        """评估当前空间状态。

        Returns:
            "ample" | "tight" | "critical"
        """
        free = 1.0 - usage_ratio
        if free > cls.AMPLE_THRESHOLD:
            return "ample"
        elif free > cls.TIGHT_THRESHOLD:
            return "tight"
        else:
            return "critical"

    @classmethod
    def can_call_llm(cls, usage_ratio: float) -> bool:
        """是否有足够空间调用 LLM 进行压缩。"""
        return cls.evaluate(usage_ratio) != "critical"


# ── 危急模式处理 ────────────────────────────────────────────


def handle_crisis(
    older: list,
    recent: list,
    strategy: CompressionStrategy,
    current_index: int,
) -> tuple[list, str]:
    """在空间危急（无法调用 LLM）时对 older 消息做分层截断。

    Args:
        older: 需要压缩的旧消息列表。
        recent: 保留的最近消息列表。
        strategy: 当前 tier 的压缩策略。
        current_index: 当前最新轮次序号。

    Returns:
        (new_history, summary_message) — 新的历史列表和描述字符串。
    """
    action = strategy.crisis_action
    tier = strategy.tier

    if action == "drop":
        # Q1: 直接丢弃 older
        return list(recent), "（低优先级对话已丢弃）"

    elif action == "label":
        # Q2: 单行标注
        n = len(older)
        label_turn = type(older[0])(
            role="system",
            content=f"[已丢弃 {n} 条低优先级对话]",
            task_tier=tier,
            turn_type="system",
        ) if older else None
        if label_turn:
            return [label_turn] + list(recent), f"（{n} 条低优先级对话已丢弃）"
        return list(recent), ""

    elif action == "auto_summary":
        # Q3: 使用 _auto_summary() 正则兜底
        from xenon.repl.context_manager import ContextManager
        dummy = ContextManager()
        dummy.history = list(older)
        summary = dummy._auto_summary(messages=list(older))
        summary_turn = type(older[0])(
            role="system",
            content=f"[对话历史摘要（本地提取）]\n\n{summary}",
            task_tier=tier,
            turn_type="system",
        ) if older else None
        if summary_turn:
            return [summary_turn] + list(recent), summary
        return list(recent), summary

    elif action == "structured_truncate":
        # Q4: 结构化截断 — 保留头尾 + 最重要 N 条
        return _structured_truncate(older, recent, strategy, current_index)

    elif action == "cross_tier_evict":
        # Q5: 跨 tier 驱逐
        return _cross_tier_evict(older, recent, strategy, current_index)

    else:
        # 未知 action → 回退到 Q3 行为
        from xenon.repl.context_manager import ContextManager
        dummy = ContextManager()
        dummy.history = list(older)
        summary = dummy._auto_summary(messages=list(older))
        summary_turn = type(older[0])(
            role="system",
            content=f"[对话历史摘要]\n\n{summary}",
            task_tier=tier,
            turn_type="system",
        ) if older else None
        return ([summary_turn] + list(recent)) if summary_turn else (list(recent), summary)


def _structured_truncate(
    older: list,
    recent: list,
    strategy: CompressionStrategy,
    current_index: int,
) -> tuple[list, str]:
    """结构化截断：对每条 older 消息保留头尾 + 按重要性排序保留 top N。"""
    head_chars = strategy.crisis_truncate_head
    tail_chars = strategy.crisis_truncate_tail
    keep_n = strategy.crisis_keep_top_n

    truncated = []
    for turn in older:
        content = turn.content
        if len(content) <= head_chars + tail_chars + 100:
            truncated.append(turn)
            continue
        head = content[:head_chars]
        tail = content[-tail_chars:]
        new_content = f"{head}\n…（省略中间 {len(content) - head_chars - tail_chars} 字符）…\n{tail}"
        # Create a new turn with truncated content
        import copy
        new_turn = copy.copy(turn)
        new_turn.content = new_content
        truncated.append(new_turn)

    # 按重要性排序，保留 top N
    if len(truncated) > keep_n:
        from xenon.repl.context_strategies import ImportanceCalculator
        scored = [
            (ImportanceCalculator.effective_importance(
                getattr(t, "task_tier", 3),
                getattr(t, "turn_index", current_index),
                current_index,
                strategy.decay_rate,
            ), t)
            for t in truncated
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        truncated = [t for _, t in scored[:keep_n]]

    n_orig = len(older)
    n_kept = len(truncated)
    summary = f"（空间不足，从 {n_orig} 条历史中结构化截断保留 {n_kept} 条）"
    return truncated + list(recent), summary


def _cross_tier_evict(
    older: list,
    recent: list,
    strategy: CompressionStrategy,
    current_index: int,
) -> tuple[list, str]:
    """跨 tier 驱逐：优先丢弃低 tier 消息，保留高 tier 消息。

    Q5 危急时的特殊机制：按 task_tier 分组，从 Q1 开始逐级驱逐。
    """
    # 按 tier 分组
    tiers: dict[int, list] = {1: [], 2: [], 3: [], 4: [], 5: []}
    for turn in older:
        t = getattr(turn, "task_tier", 3)
        tiers.setdefault(t, []).append(turn)

    kept = []
    dropped_count = 0
    labeled_count = 0

    # 从 Q1 开始逐级处理
    for t in [1, 2, 3, 4, 5]:
        if t == 1:
            dropped_count += len(tiers.get(t, []))
            # Q1: 直接丢弃
        elif t == 2:
            labeled_count += len(tiers.get(t, []))
            # Q2: 单行标注（丢弃内容）
        elif t == 3:
            # Q3: 正则摘要
            if tiers.get(t):
                from xenon.repl.context_manager import ContextManager
                dummy = ContextManager()
                dummy.history = list(tiers[t])
                summary = dummy._auto_summary(messages=list(tiers[t]))
                template_turn = tiers[t][0]
                kept.append(type(template_turn)(
                    role="system",
                    content=f"[Q3 任务摘要]\n\n{summary}",
                    task_tier=3,
                    turn_type="system",
                ))
        else:
            # Q4-Q5: 保留但结构化截断
            for turn in tiers.get(t, []):
                content = turn.content
                if len(content) > strategy.crisis_truncate_head + strategy.crisis_truncate_tail + 100:
                    import copy
                    new_turn = copy.copy(turn)
                    new_turn.content = (
                        content[:strategy.crisis_truncate_head]
                        + f"\n…（省略中间 {len(content) - strategy.crisis_truncate_head - strategy.crisis_truncate_tail} 字符）…\n"
                        + content[-strategy.crisis_truncate_tail:]
                    )
                    kept.append(new_turn)
                else:
                    kept.append(turn)

    parts = []
    if dropped_count:
        parts.append(f"丢弃 {dropped_count} 条 Q1 对话")
    if labeled_count:
        parts.append(f"压缩 {labeled_count} 条 Q2 对话")
    summary = f"（空间危急：{', '.join(parts)}，保留 {len(kept)} 条重要对话）" if parts else ""
    return kept + list(recent), summary


# ── 便捷函数 ────────────────────────────────────────────────


# 模块级单例
_selector: TieredStrategySelector | None = None
_classifier: ToolOutputClassifier | None = None


def get_selector() -> TieredStrategySelector:
    """获取策略选择器单例。"""
    global _selector
    if _selector is None:
        _selector = TieredStrategySelector()
    return _selector


def get_classifier() -> ToolOutputClassifier:
    """获取工具输出分类器单例。"""
    global _classifier
    if _classifier is None:
        _classifier = ToolOutputClassifier()
    return _classifier
