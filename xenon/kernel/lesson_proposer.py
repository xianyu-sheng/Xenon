"""Lesson proposer: turn harness-judged failures into proposals (slice 2).

Guardrail 1 in code: only deterministic events propose lessons.  The templates
are deterministic strings derived from the event payload — never the model's
prose, never an LLM call.  The runtime writes ``LESSON_PROPOSED`` at the
failure point; a later slice decides whether to store and apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from xenon.kernel.lessons import lesson_fingerprint

__all__ = ["LessonProposal", "propose_lesson"]


@dataclass(frozen=True, slots=True)
class LessonProposal:
    kind: str  # pitfall / parameter / preference
    trigger_event: str
    fingerprint: str
    action_text: str
    source_task: str = ""
    counterfactual: str = ""


def _canonical_payload(event_kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """The deterministic subset a fingerprint is built from.

    The runtime proposes with a subset (kind/target/reason) and the post-task
    recurrence scan sees the *full* event payload (with obligation_id, stage,
    attempts…).  Both must compute the same fingerprint, so the subset is the
    canonical shape for every event kind — extra fields never change a lesson's
    identity.
    """

    if event_kind == "policy_decision":
        return {
            "kind": str(payload.get("kind") or ""),
            "capability": str(
                payload.get("capability") or payload.get("tool") or ""
            ),
            "reason": str(payload.get("reason") or ""),
        }
    if event_kind == "debt_opened":
        return {
            "kind": str(payload.get("kind") or ""),
            "target": str(payload.get("target") or ""),
            "reason": str(payload.get("reason") or ""),
        }
    if event_kind == "loop_detected":
        return {
            "signal": str(payload.get("signal") or ""),
            "reason": str(payload.get("reason") or ""),
        }
    if event_kind == "user_approval":
        return {
            "approved": bool(payload.get("approved")),
            "tool": str(payload.get("tool") or ""),
            "reason": str(payload.get("reason") or ""),
        }
    return dict(payload)


def propose_lesson(
    event_kind: str,
    payload: Mapping[str, Any],
    *,
    source_task: str = "",
) -> LessonProposal | None:
    """Build a proposal from one harness event, or ``None`` when the event is
    not a lesson source.  ``action_text`` is a template over the payload, so
    the same failure always proposes the same lesson."""

    if event_kind == "policy_decision":
        decision = str(payload.get("kind") or "")
        if decision != "veto":
            return None
        capability = str(payload.get("capability") or payload.get("tool") or "?")
        reason = str(payload.get("reason") or "policy veto")
        return LessonProposal(
            kind="pitfall",
            trigger_event="policy_decision",
            fingerprint=lesson_fingerprint("policy_decision", _canonical_payload("policy_decision", payload)),
            action_text=f"capability {capability} 在本任务被策略拒绝（{reason}）；"
            f"下次先确认该能力被允许",
            counterfactual="该能力有显式授权或策略已改变",
            source_task=source_task,
        )
    if event_kind == "debt_opened":
        obligation = str(payload.get("kind") or "?")
        target = str(payload.get("target") or "")
        reason = str(payload.get("reason") or "no evidence")
        target_part = f" {target}" if target else ""
        return LessonProposal(
            kind="pitfall",
            trigger_event="debt_opened",
            fingerprint=lesson_fingerprint("debt_opened", _canonical_payload("debt_opened", payload)),
            action_text=f"声称 {obligation}{target_part} 前必须存在对应工具回执"
            f"（{reason}）；不要凭空声称",
            counterfactual="回执机制改变或该声称允许用户接受债务",
            source_task=source_task,
        )
    if event_kind == "loop_detected":
        signal = str(payload.get("signal") or "loop")
        reason = str(payload.get("reason") or "repeated call")
        return LessonProposal(
            kind="parameter",
            trigger_event="loop_detected",
            fingerprint=lesson_fingerprint("loop_detected", _canonical_payload("loop_detected", payload)),
            action_text=f"循环信号 {signal}（{reason}）——同 profile 下次任务"
            f"应降低该能力调用预算或换策略",
            counterfactual="任务本身就是重复性劳动（如实测多次运行同一命令）",
            source_task=source_task,
        )
    if event_kind == "user_approval":
        approved = bool(payload.get("approved"))
        if approved:
            return None
        tool = str(payload.get("tool") or "?")
        reason = str(payload.get("reason") or "user denied")
        return LessonProposal(
            kind="preference",
            trigger_event="user_approval",
            fingerprint=lesson_fingerprint("user_approval", _canonical_payload("user_approval", payload)),
            action_text=f"{tool} 调用被用户拒绝（{reason}）——下次避免同类调用",
            counterfactual="用户后续显式允许了同类调用",
            source_task=source_task,
        )
    return None
