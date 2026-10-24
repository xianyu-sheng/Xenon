"""TurnGate: 回合级唯一校验层（三层可信度模型）。

- L0 证据层：append-only 哈希链（evidence ledger，独立层），只证过程真实。
- L1 结构层（本门阻断类）：空洞回答——确定性、无语义盲点，命中才重试/熔断。
- L2 语义层（本门提醒类）：宣称审计、完成准则、文件声称、写入后验证——
  存在先天盲点（内容语义无穷），**不再阻断发布**；命中时生成
  「待人工核验清单」随答案一起输出，让用户自行核对。

判定：pass 发布 / fail（结构层）反馈重试 / remind（语义层）发布+提醒 /
fuse 同因结构失败熔断。确定性组件：无 LLM 调用、不授予权限。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from xenon.engine.evidence_contracts import WRITE_TOOL_NAMES
from xenon.engine.hollow_detector import HollowDetector
from xenon.engine.task_verifier import (
    _FAILURE_CLAIM,
    _SUCCESS_CLAIM,
    verify_final_answer,
)

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_REMIND = "remind"
VERDICT_FUSE = "fuse"

# 可发布（附提醒或不附）的判定。
PUBLISHABLE_OUTCOMES = frozenset({VERDICT_PASS, VERDICT_REMIND})


@dataclass
class GateVerdict:
    outcome: str  # pass | fail | fuse
    reasons: list[str] = field(default_factory=list)
    retry_budget_hint: int = 10  # 重试轮步数下限（实际=首轮步数/2 再 clamp）


@dataclass
class _RetryRound:
    signature: tuple[str, ...]




class _EventTracker:
    """把回合工具回执适配成 tracker 形状（供 verify_file_claims 复用）。"""

    def __init__(self, events: list[dict]) -> None:
        self.calls = [
            type(
                "_Call",
                (),
                {
                    "tool_name": e.get("tool"),
                    "params": e.get("params") or {},
                    "success": bool(e.get("success")),
                },
            )()
            for e in events
        ]


class TurnGate:
    """回合级校验状态机：evaluate 一次 → 单一判定。

    ``max_same_cause_failures``：连续 N 轮校验原因完全相同 → fuse
    （原地打转检测，如“每次都宣称成功但有失败回执”）。
    """

    def __init__(self, max_same_cause_failures: int | None = None) -> None:
        if max_same_cause_failures is None:
            # XENON_GATE_FUSE_THRESHOLD 可调（默认 2，无效值回退）。
            import os

            try:
                max_same_cause_failures = max(
                    1, int(os.environ.get("XENON_GATE_FUSE_THRESHOLD", "2"))
                )
            except ValueError:
                max_same_cause_failures = 2
        self.max_same_cause_failures = max_same_cause_failures
        self._rounds: list[_RetryRound] = []

    def reset(self) -> None:
        self._rounds.clear()

    def evaluate(
        self,
        answer: str,
        *,
        tool_events: list[dict] | tuple[dict, ...] = (),
        criteria: list[str] | tuple[str, ...] = (),
    ) -> GateVerdict:
        events = list(tool_events)
        structural: list[str] = []  # L1 结构层：命中才阻断/重试/熔断
        reminders: list[str] = []  # L2 语义层：发布 + 待人工核验清单

        # L1) 空洞回答：确定性结构判定，无语义盲点。
        if events or _SUCCESS_CLAIM.search(answer or ""):
            hr = HollowDetector().detect(answer or "", len(events))
            if hr.is_hollow:
                hits = list(getattr(hr, "hits", []) or [])
                structural.append(
                    f"回答空洞（{hits[0] if hits else '无实质内容'}）"
                )

        # L2) 宣称审计 + 完成准则（task_verifier 规则源）。
        ok, base_reasons = verify_final_answer(
            answer or "", criteria=list(criteria), tool_events=events
        )
        reminders.extend(base_reasons)

        # L2) 文件声称审计：存在读写盲点，只提醒不阻断。
        try:
            from xenon.engine.evidence_gate import verify_file_claims

            passed, unverified = verify_file_claims(
                answer or "", _EventTracker(events)
            )
            if not passed:
                reminders.append(
                    f"声称创建但未经工具验证的文件: {', '.join(unverified)}（如为远程读取请忽略）"
                )
        except Exception:  # noqa: BLE001 — 规则失败不影响其他规则
            pass

        # L2) 写入后验证失败且未披露。
        has_write = any(
            e.get("tool") in WRITE_TOOL_NAMES and e.get("success") for e in events
        )
        failed_cmd = any(
            e.get("tool") == "command" and not e.get("success") for e in events
        )
        ok_cmd = any(
            e.get("tool") == "command" and e.get("success") for e in events
        )
        if has_write and failed_cmd and not ok_cmd and not _FAILURE_CLAIM.search(
            answer or ""
        ):
            reminders.append("写入后验证命令失败且回答未披露，建议人工复核")

        # 结构层优先：命中 → 阻断/重试/熔断（同因比较只基于结构层）。
        if structural:
            signature = tuple(sorted(structural))
            if self._rounds and self._rounds[-1].signature == signature:
                self._rounds.append(_RetryRound(signature))
                if len(self._rounds) >= self.max_same_cause_failures:
                    self.reset()
                    head = structural[0]
                    return GateVerdict(
                        VERDICT_FUSE,
                        reasons=[
                            f"连续 {self.max_same_cause_failures} 轮结构校验原因相同"
                            f"（{head}），熔断以免原地打转"
                        ],
                    )
            else:
                self._rounds = [_RetryRound(signature)]
            return GateVerdict(VERDICT_FAIL, reasons=list(structural))

        if reminders:
            self.reset()
            return GateVerdict(VERDICT_REMIND, reasons=list(reminders))

        self.reset()
        return GateVerdict(VERDICT_PASS)

    @property
    def rounds_used(self) -> int:
        return len(self._rounds)


def tool_events_from_panel(panel: Any) -> list[dict]:
    """从 thinking 面板提取工具回执（TurnGate 的唯一输入面）。"""

    events: list[dict] = []
    for step in getattr(panel, "steps", []) or []:
        if not getattr(step, "action", None):
            continue
        events.append(
            {
                "tool": str(step.action),
                "success": not bool(step.is_error),
                "error": str(getattr(step, "observation", "") or "")[:120],
                "params": dict(getattr(step, "action_input", {}) or {}),
            }
        )
    return events
