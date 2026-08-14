"""
VerificationLoop — 引擎层跨轮次验证循环公共组件（v0.8.3）。

将 v0.8.2 的 Plan-Execute 单机验证闭环提升为引擎层通用组件，
7 种推理范式统一接入。核心机制：

1. **跨轮次状态传递**：每轮执行后捕获 ExecutionEvidence，按成功/失败
   分类累积（失败时间线 + 成功缓存），注入下一轮 prompt。
2. **证据时效语义**：文件被修改 → 该文件写入证据失效；测试重跑 →
   旧通过证据标记为被覆盖。
3. **动态终止**：无进展检测（连续 2-3 轮失败摘要相同）→ fail-closed；
   收敛检测（摘要逐轮变化变短）→ 继续。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from xenon.engine.execution_evidence import (
    ExecutionEvidence,
    _TEST_COMMAND,
)
from xenon.engine.evidence_gate import WRITE_TOOL_NAMES as _WRITE_TOOL_NAMES
from xenon.engine.tool_tracker import ToolCall

logger = logging.getLogger(__name__)


@dataclass
class _EvidenceEntry:
    """A single piece of verified evidence, with expiry tracking."""
    evidence_id: str
    category: str  # "test_pass" | "file_write" | "tool_call"
    target: str  # file path, test command, or tool name
    summary: str  # human-readable summary (< 200 chars)
    source_round: int
    valid: bool = True


@dataclass
class _RoundRecord:
    """Record of one verification loop round."""
    round: int
    failed_test_summary: str  # structured summary of failures
    raw_failure_detail: str  # full failure output (truncated)
    outcome: str  # "fixed" | "still_failing" | "stuck"
    evidence_id: str | None  # evidence produced (if any)


def _extract_failure_summary(evidence: ExecutionEvidence) -> str:
    """Extract a structured failure summary from execution evidence.

    Returns a compact string capturing failure type, key assertion/error,
    and affected files — designed for cross-round accumulation.
    """
    parts: list[str] = []
    for call in evidence.failed_calls:
        err = call.error or call.result_summary or ""
        # Extract key assertion line or error type
        lines = err.split("\n")
        key_line = ""
        for line in lines[:15]:
            stripped = line.strip()
            if any(kw in stripped for kw in ("AssertionError", "Error:", "FAILED",
                                              "assert ", "TypeError", "NameError",
                                              "ModuleNotFoundError", "ImportError")):
                key_line = stripped[:150]
                break
        if not key_line and lines:
            key_line = lines[0][:150]
        if key_line:
            parts.append(key_line)
    if not parts:
        parts.append("(no structured failure detail)")
    return " | ".join(parts[:5])  # cap at 5 failures


def _should_verify(evidence: ExecutionEvidence, user_input: str) -> bool:
    """Determine whether verification is needed.

    Conditions:
    1. Task requires write operations
    2. Tracker has write attempts (success OR failure)
    3. Has failed command(s) — 不限于测试命令；写代码后任何失败命令
      都可能反映验证失败（如 python -c 内联验证、python script.py 运行脚本）
    4. No successful test commands — 若已有成功测试说明修复已通过

    v0.8.3 实测：LLM 常用 python -c / python script.py 做内联验证，
    这些命令不匹配 _TEST_COMMAND（pytest/unittest 等），导致验证
    循环漏掉「写成功但验证失败」的场景。改为任何失败命令都触发。
    """
    from xenon.engine.evidence_gate import task_requires_write
    if not task_requires_write(user_input):
        return False
    has_write_attempt = any(
        c.tool_name in _WRITE_TOOL_NAMES for c in evidence.calls
    )
    if not has_write_attempt:
        return False
    if evidence.successful_tests:
        return False
    has_failed_command = any(not c.success for c in evidence.calls)
    if not has_failed_command:
        return False
    return True


class VerificationLoop:
    """引擎层跨轮次验证循环组件。

    各引擎在执行循环尾部调用 ``feed()``，组件自动判断是否需要进入验证循环、
    是否继续、缓存哪些证据。
    """

    def __init__(
        self,
        max_rounds: int = 8,
        max_steps: int = 10,
        stuck_threshold: int = 3,
    ) -> None:
        self.max_rounds = max_rounds
        self.max_steps = max_steps
        self.stuck_threshold = stuck_threshold
        self.reset()

    # ── Public API ────────────────────────────────────────────────

    def reset(self) -> None:
        """Reset state for a new run."""
        self.round_count: int = 0
        self.failure_timeline: list[_RoundRecord] = []
        self.success_cache: dict[str, _EvidenceEntry] = {}
        self._last_failure_summary: str = ""
        self._stuck_rounds: int = 0
        self._active: bool = False
        self._engine: Any = None  # set by engine during __init__

    def feed(
        self,
        evidence: ExecutionEvidence,
        user_input: str,
    ) -> str | None:
        """Feed execution evidence into the verification loop.

        Returns a repair prompt string if verification is needed,
        or ``None`` if no further verification is required.

        The engine is responsible for executing the repair (how depends
        on the engine type) and then calling ``record_outcome()``.
        """
        if not self._active:
            return None

        # Check if verification is needed at all
        if not _should_verify(evidence, user_input):
            self._active = False
            return None

        # Check budget
        if self.round_count >= self.max_rounds:
            logger.warning(
                f"VerificationLoop: 已达轮次上限 {self.max_rounds}，终止"
            )
            self._active = False
            return None

        if self._stuck_rounds >= self.stuck_threshold:
            logger.warning(
                f"VerificationLoop: 连续 {self.stuck_threshold} 轮无进展，终止"
            )
            self._active = False
            return None

        # Build the repair prompt
        summary = _extract_failure_summary(evidence)
        repair_prompt = self._build_repair_prompt(evidence, summary)

        # Track for no-progress detection
        if summary == self._last_failure_summary:
            self._stuck_rounds += 1
        else:
            self._stuck_rounds = 0
        self._last_failure_summary = summary

        # Check stuck AFTER incrementing counter
        if self._stuck_rounds >= self.stuck_threshold:
            logger.warning(
                f"VerificationLoop: 连续 {self.stuck_threshold} 轮无进展，终止"
            )
            self._active = False
            return None

        return repair_prompt

    def record_outcome(
        self,
        evidence: ExecutionEvidence,
        outcome: str,
        evidence_id: str | None = None,
    ) -> None:
        """Record the outcome of a verification round.

        Args:
            evidence: ExecutionEvidence from the repair round.
            outcome: ``"fixed"``, ``"still_failing"``, or ``"stuck"``.
            evidence_id: Optional evidence ID for the repair outcome.
        """
        self.round_count += 1

        # Build round record
        summary = _extract_failure_summary(evidence)
        # Get raw failure detail from failed calls
        raw_detail = ""
        for call in evidence.failed_calls:
            detail = call.error or call.result_summary or ""
            if detail:
                raw_detail = detail[:500]
                break

        record = _RoundRecord(
            round=self.round_count,
            failed_test_summary=summary,
            raw_failure_detail=raw_detail,
            outcome=outcome,
            evidence_id=evidence_id,
        )
        self.failure_timeline.append(record)

        # Invalidate stale evidence first (old entries from previous rounds),
        # then add new entries from this round (so new entries aren't
        # immediately invalidated).
        self._invalidate_stale_evidence(evidence)
        self._update_success_cache(evidence, self.round_count)

        # Determine if we should continue
        if outcome == "fixed" and evidence.successful_tests:
            self._active = False
        elif outcome == "stuck":
            self._stuck_rounds += 1

    @property
    def should_continue(self) -> bool:
        """Whether the engine should continue the verification loop."""
        return self._active

    @property
    def is_stuck(self) -> bool:
        """Whether the loop is stuck (no progress detected)."""
        return self._stuck_rounds >= self.stuck_threshold

    @property
    def total_rounds_used(self) -> int:
        """Total verification rounds used in this run."""
        return self.round_count

    def build_context_summary(self) -> str:
        """Build a cross-round context summary for the next round's prompt.

        Includes failure timeline (recent full, earlier compressed) and
        success cache entries.
        """
        lines: list[str] = []
        if self.failure_timeline:
            lines.append("【验证循环历史】")
            # Recent 3 rounds: full detail
            for rec in self.failure_timeline[-3:]:
                lines.append(
                    f"  R{rec.round}: {rec.outcome} — {rec.failed_test_summary[:200]}"
                )
            # Earlier rounds: compressed
            for rec in self.failure_timeline[:-3]:
                lines.append(
                    f"  R{rec.round}: {rec.outcome} (压缩)"
                )
        # Success cache
        valid_entries = [e for e in self.success_cache.values() if e.valid]
        if valid_entries:
            lines.append("【已验证可复用的操作】")
            for entry in valid_entries[:10]:
                lines.append(
                    f"  [{entry.evidence_id}] {entry.category}: {entry.summary[:150]}"
                )
            lines.append("（以上步骤已确定性验证通过，无需重做）")
        return "\n".join(lines)

    # ── Internal ─────────────────────────────────────────────────

    def _build_repair_prompt(
        self,
        evidence: ExecutionEvidence,
        summary: str,
    ) -> str:
        """Build the repair prompt for a verification round.

        Includes failure detail, context summary (cross-round state),
        and success cache guidance.
        """
        # Get raw failure detail
        raw_detail = ""
        for call in evidence.failed_calls:
            detail = call.error or call.result_summary or ""
            if detail:
                raw_detail = detail[:1000]
                break

        context = self.build_context_summary()

        prompt = (
            "【验证闭环】文件已修改，但测试命令失败。\n\n"
            f"失败输出：{raw_detail}\n\n"
            f"{context}\n\n"
            "请：1) 读取测试失败的具体断言/错误信息；2) 定位根因并修改代码；"
            "3) 重新运行测试命令验证通过；4) 确认通过后再给出最终总结。\n"
            "不要重复已经验证通过的操作（见上方「已验证可复用的操作」列表）。"
        )
        return prompt

    def _update_success_cache(
        self,
        evidence: ExecutionEvidence,
        source_round: int,
    ) -> None:
        """Update success cache with evidence from this round."""
        for call in evidence.calls:
            if not call.success:
                continue
            eid = self._make_evidence_id(call, source_round)
            if call.tool_name in _WRITE_TOOL_NAMES:
                from xenon.engine.execution_evidence import _call_paths
                for path in _call_paths(call):
                    key = f"file_write:{path}"
                    self.success_cache[key] = _EvidenceEntry(
                        evidence_id=eid,
                        category="file_write",
                        target=path,
                        summary=f"写入 {path}",
                        source_round=source_round,
                    )
            elif call.tool_name == "command":
                cmd = str(call.params.get("command") or call.params.get("cmd") or "")
                if _TEST_COMMAND.search(cmd):
                    cmd_short = cmd[:200]
                    key = f"test_pass:{cmd_short}"
                    self.success_cache[key] = _EvidenceEntry(
                        evidence_id=eid,
                        category="test_pass",
                        target=cmd_short,
                        summary=f"测试通过: {cmd[:120]}",
                        source_round=source_round,
                    )

    def _invalidate_stale_evidence(self, evidence: ExecutionEvidence) -> None:
        """Invalidate evidence that is no longer valid.

        - File was modified → previous file_write evidence for that file expires.
        - Test was re-run → previous test_pass evidence for that command expires.
        """
        # File writes in this round invalidate earlier file_write evidence for same file
        for call in evidence.calls:
            if not call.success:
                continue
            if call.tool_name in _WRITE_TOOL_NAMES:
                from xenon.engine.execution_evidence import _call_paths
                for path in _call_paths(call):
                    key = f"file_write:{path}"
                    if key in self.success_cache:
                        self.success_cache[key].valid = False
                        logger.debug(
                            f"VerificationLoop: 证据 {key} 失效（文件被重写）"
                        )
            if call.tool_name == "command":
                cmd = str(call.params.get("command") or call.params.get("cmd") or "")
                if _TEST_COMMAND.search(cmd):
                    for key, entry in list(self.success_cache.items()):
                        if key.startswith("test_pass:") and entry.target == cmd[:200]:
                            entry.valid = False
                            logger.debug(
                                f"VerificationLoop: 证据 {key} 失效（测试重跑）"
                            )

    @staticmethod
    def _make_evidence_id(call: ToolCall, source_round: int) -> str:
        """Generate a deterministic evidence ID from a tool call."""
        cmd = str(call.params.get("command") or call.params.get("cmd") or "")
        path = call.params.get("file_path") or call.params.get("path") or ""
        tool = call.tool_name
        return f"R{source_round}_{tool}_{cmd[:20]}_{path[:20]}" .strip("_")