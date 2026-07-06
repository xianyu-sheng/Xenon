"""HollowDetector — 空洞回答检测（F2 / §Q2）。

规范 Q2 的"15 正则 + 组合判定"：识别 LLM 输出"描述代替执行 / 套话堆砌 / 答非所问"
的空洞回答，配合 BudgetManager.on_hollow_answer() 给补救轮次。

三类信号（任一成立即判空洞）：

1. **快速失败**：``len < 5`` —— 过短不可能有实质内容；
2. **不成比例**：``tool_calls >= 5 且 len < 100`` —— 做了大量工具调用却几乎不汇报，
   典型"执行了但不总结"；
3. **正则 + 组合判定**：命中 15 个反模式正则之一 **且**（长度不足 ``< min_length``
   或 结构差 ``无代码块/路径/URL/内联代码``）。

组合判定的 ``AND`` 是为降假阳：合法详细总结可能恰好含"综上所述"，但只要它够长
**且**有代码/路径等实质结构，就不判空洞——只拦"短而空"或"长但全是套话"。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── 15 个反模式正则 ──────────────────────────────────────────
# (名称, 编译后的正则)。命中"套话/承诺/ advisory"类表述。
_HOLLOW_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("接下来我将",        re.compile(r"接下来我将")),
    ("基于以上分析",      re.compile(r"基于以上(分析|内容|讨论|步骤)")),
    ("整体设计完善",      re.compile(r"整体(设计|架构|方案).{0,4}(完善|合理|清晰|完整|健全)")),
    ("综上所述",          re.compile(r"综上所述")),
    ("总而言之",          re.compile(r"总而言之")),
    ("需要注意的是",      re.compile(r"需要注意(的是)?")),
    ("在此基础上",        re.compile(r"在此基础上")),
    ("通过以上",          re.compile(r"通过以上")),
    ("如下所示",          re.compile(r"如下所示")),
    ("具体如下",          re.compile(r"具体如下")),
    ("我认为/我觉得",     re.compile(r"我(认为|觉得|个人理解)")),
    ("建议你/您",         re.compile(r"建议(你|您)")),
    ("可以尝试",          re.compile(r"可以尝试")),
    ("首先其次最后",      re.compile(r"首先[，,、].{0,40}(其次|然后).{0,40}(最后|最终)")),
    ("省略号填充",        re.compile(r"…{2,}|\.{4,}|……+")),
]

# 实质内容标记：代码块 / 文件路径 / URL / 有内容的内联代码 / 命令
_CODE_BLOCK = re.compile(r"```")
_INDENTED_CODE = re.compile(r"(?m)^\s{4,}\S")
_FILE_PATH = re.compile(
    r"[\w\-./\\]+\.(py|js|ts|tsx|jsx|java|go|rs|c|cpp|h|hpp|rb|php|md|txt|json|"
    r"yaml|yml|sh|bat|ps1|toml|ini|cfg|html|css|sql)\b",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://\S+")
_INLINE_CODE = re.compile(r"`[^`\n]{4,}`")


@dataclass
class HollowResult:
    """空洞检测结果。"""

    is_hollow: bool
    reasons: list[str] = field(default_factory=list)
    score: float = 0.0  # 0-1，越大越确信空洞
    hits: list[str] = field(default_factory=list)  # 命中的正则名

    def hint(self) -> str:
        """生成补救提示，供引擎注入 messages 强制 LLM 产出具体内容。"""
        if not self.is_hollow:
            return ""
        parts = ["⚠️ 你的回答被判为空洞，请重新给出**具体内容**："]
        if any("quick_fail" in r for r in self.reasons):
            parts.append("- 回答过短，必须包含实质信息（代码/路径/数据/命令）。")
        if any("disproportionate" in r for r in self.reasons):
            parts.append("- 你已执行多次工具，请基于工具结果详细汇报，而非一句带过。")
        if self.hits:
            parts.append(f"- 避免套话表述（命中: {', '.join(self.hits)}），"
                         "直接给出代码块、文件路径或可执行步骤。")
        parts.append("若任务确已完成，请在 final_answer 中附上产物（文件路径/代码/命令输出）。")
        return "\n".join(parts)


class HollowDetector:
    """空洞回答检测器。

    Args:
        min_length: 正则分支的"长度不足"阈值，默认 200。
        quick_fail_len: 快速失败阈值，默认 5。
        disproportionate_tools: 不成比例检查的工具调用下限，默认 5。
        disproportionate_len: 不成比例检查的字符上限，默认 100。

    用法::

        hd = HollowDetector()
        result = hd.detect(final_answer, tool_call_count=len(tracker.calls))
        if result.is_hollow:
            budget.on_hollow_answer()
            messages.append({"role": "user", "content": result.hint()})
    """

    def __init__(
        self,
        *,
        min_length: int = 200,
        quick_fail_len: int = 5,
        disproportionate_tools: int = 5,
        disproportionate_len: int = 100,
    ) -> None:
        self.min_length = min_length
        self.quick_fail_len = quick_fail_len
        self.disproportionate_tools = disproportionate_tools
        self.disproportionate_len = disproportionate_len

    # ── 实质内容判定 ──────────────────────────────────────────

    def has_substance(self, text: str) -> bool:
        """是否含实质结构：代码块/缩进代码/文件路径/URL/有内容内联代码。"""
        if not text:
            return False
        return bool(
            _CODE_BLOCK.search(text)
            or _INDENTED_CODE.search(text)
            or _FILE_PATH.search(text)
            or _URL.search(text)
            or _INLINE_CODE.search(text)
        )

    # ── 主检测 ────────────────────────────────────────────────

    def detect(self, text: str | None, tool_call_count: int = 0) -> HollowResult:
        """检测回答是否空洞。

        Returns:
            HollowResult：``is_hollow`` / ``reasons`` / ``score`` / ``hits``。
        """
        # 空文本
        if not text or not text.strip():
            return HollowResult(
                is_hollow=True,
                reasons=["quick_fail: 回答为空"],
                score=1.0,
            )

        stripped = text.strip()
        n = len(stripped)
        reasons: list[str] = []
        hits: list[str] = []
        score = 0.0

        # ① 快速失败：< quick_fail_len
        if n < self.quick_fail_len:
            reasons.append(f"quick_fail: 回答过短({n}字符<{self.quick_fail_len})")
            score = 1.0
            logger.info(f"HollowDetector: quick_fail ({n} chars)")
            return HollowResult(True, reasons, score, hits)

        # ② 不成比例：tool_calls 多但回答极短
        if tool_call_count >= self.disproportionate_tools and n < self.disproportionate_len:
            reasons.append(
                f"disproportionate: {tool_call_count}次工具调用但回答仅{n}字符"
                f"(<{self.disproportionate_len})"
            )
            score = max(score, 0.9)

        # ③ 正则 + 组合判定：命中正则 且 (长度不足 或 结构差)
        hits = [name for name, pat in _HOLLOW_PATTERNS if pat.search(stripped)]
        if hits:
            length_insufficient = n < self.min_length
            struct_poor = not self.has_substance(stripped)
            if length_insufficient or struct_poor:
                tags = []
                if length_insufficient:
                    tags.append(f"长度不足<{self.min_length}")
                if struct_poor:
                    tags.append("结构差(无代码/路径/URL)")
                reasons.append(
                    f"regex: 命中{hits} 且 ({'/'.join(tags)})"
                )
                # 分数：基础 0.7 + 每多命中一个 +0.05，封顶 0.95
                score = max(score, min(0.95, 0.7 + 0.05 * len(hits)))

        is_hollow = bool(reasons)
        if is_hollow:
            logger.info(
                f"HollowDetector: 判定空洞 (score={score:.2f}, "
                f"len={n}, tools={tool_call_count}, hits={hits})"
            )
        return HollowResult(is_hollow, reasons, round(score, 3), hits)


# 模块级便捷实例（无状态，可共享）
DEFAULT_DETECTOR = HollowDetector()
