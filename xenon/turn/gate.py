"""TurnGate: 回合级唯一校验层（发布门，按会话轮次设计）。

所有校验语义收敛于此：宣称审计、完成准则、无进展/同因失败熔断。
引擎内部不再有校验门；回合收尾时 REPL 调用一次，得到 GateVerdict：
pass 发布 / fail 反馈重试 / fuse 熔断（原地打转时不再消耗预算）。

确定性组件：无 LLM 调用、不授予权限。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from xenon.engine.task_verifier import verify_final_answer

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
        ok, reasons = verify_final_answer(
            answer or "", criteria=list(criteria), tool_events=list(tool_events)
        )
        if ok:
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
            }
        )
    return events
