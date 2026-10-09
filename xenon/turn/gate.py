"""TurnGate: 回合级唯一校验层（发布门，按会话轮次设计）。

所有校验语义收敛于此：宣称审计、完成准则、文件声称审计（原引擎交付闸门）、
写入后验证（原引擎 VerificationLoop）、空洞回答（原引擎 HollowDetector）、
无进展/同因失败熔断。引擎内部不再有校验门；回合收尾时 REPL 调用一次，
得到 GateVerdict：pass 发布 / fail 反馈重试 / fuse 熔断。

确定性组件：无 LLM 调用、不授予权限。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from xenon.engine.hollow_detector import HollowDetector
from xenon.engine.task_verifier import (
    _FAILURE_CLAIM,
    _SUCCESS_CLAIM,
    verify_final_answer,
)

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_FUSE = "fuse"


@dataclass
class GateVerdict:
    outcome: str  # pass | fail | fuse
    reasons: list[str] = field(default_factory=list)
    retry_budget_hint: int = 10  # 重试轮步数下限（实际=首轮步数/2 再 clamp）


@dataclass
class _RetryRound:
    signature: tuple[str, ...]


_WRITE_TOOLS = frozenset(
    {"write_file", "edit_file", "batch_write", "create_directory"}
)


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

    def __init__(self, max_same_cause_failures: int = 2) -> None:
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
        reasons: list[str] = []

        # 1) 宣称审计 + 完成准则（task_verifier 规则源）。
        ok, base_reasons = verify_final_answer(
            answer or "", criteria=list(criteria), tool_events=events
        )
        reasons.extend(base_reasons)

        # 2) 空洞回答（原引擎 HollowDetector，回合级）：只在做过工或
        #    任务级宣称成功时检查，避免误伤短回复闲聊。
        if events or _SUCCESS_CLAIM.search(answer or ""):
            hr = HollowDetector().detect(answer or "", len(events))
            if hr.is_hollow:
                hits = list(getattr(hr, "hits", []) or [])
                reasons.append(
                    f"回答空洞（{hits[0] if hits else '无实质内容'}）"
                )

        # 3) 文件声称审计（原引擎 FileClaimGate 交付闸门，回合级）。
        try:
            from xenon.engine.evidence_gate import verify_file_claims

            passed, unverified = verify_file_claims(
                answer or "", _EventTracker(events)
            )
            if not passed:
                reasons.append(
                    f"LLM 声称创建但未经工具验证的文件: {', '.join(unverified)}"
                )
        except Exception:  # noqa: BLE001 — 规则失败不影响其他规则
            pass

        # 4) 写入后验证失败且未披露（原引擎 VerificationLoop，回合级）。
        has_write = any(
            e.get("tool") in _WRITE_TOOLS and e.get("success") for e in events
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
            reasons.append("写入后验证命令失败且回答未披露，需修复或如实说明")

        if not reasons:
            self.reset()
            return GateVerdict(VERDICT_PASS)

        signature = tuple(sorted(reasons))
        if self._rounds and self._rounds[-1].signature == signature:
            self._rounds.append(_RetryRound(signature))
            if len(self._rounds) >= self.max_same_cause_failures:
                self.reset()
                head = reasons[0] if reasons else "校验未通过"
                return GateVerdict(
                    VERDICT_FUSE,
                    reasons=[
                        f"连续 {self.max_same_cause_failures} 轮校验原因相同"
                        f"（{head}），熔断以免原地打转"
                    ],
                )
        else:
            self._rounds = [_RetryRound(signature)]
        return GateVerdict(VERDICT_FAIL, reasons=list(reasons))

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
