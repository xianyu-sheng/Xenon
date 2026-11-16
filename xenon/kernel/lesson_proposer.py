"""Lesson proposer: turn harness-judged failures into proposals (slice 2).

Guardrail 1 in code: only deterministic events propose lessons.  The templates
are deterministic strings derived from the event payload — never the model's
prose, never an LLM call.  The runtime writes ``LESSON_PROPOSED`` at the
failure point; a later slice decides whether to store and apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from xenon.kernel.lessons import (
    canonical_failure_payload,
    lesson_fingerprint,
    lesson_shape,
)

__all__ = ["LessonProposal", "propose_lesson"]


@dataclass(frozen=True, slots=True)
class LessonProposal:
    kind: str  # pitfall / parameter / preference
    trigger_event: str
    fingerprint: str
    action_text: str
    source_task: str = ""
    counterfactual: str = ""
    #: Structural family the eventual lesson belongs to.  Stored with the
    #: lesson so later tasks can decide relevance without re-deriving it from
    #: the exact payload (which no longer exists at task-build time).
    shape: str = ""


def _canonical_payload(event_kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Compatibility alias — the canonical subset lives in ``lessons``."""

    return canonical_failure_payload(event_kind, payload)


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
        canonical = _canonical_payload("policy_decision", payload)
        if canonical["kind"] != "veto":
            return None
        # Read the subject from the canonical payload, not the raw one: the
        # gate writes ``scope`` while some callers write ``capability``.  The
        # fingerprint/shape already normalized this; the wording must name the
        # same subject or the lesson reads "capability ?".
        capability = canonical["capability"] or "?"
        reason = canonical["reason"] or "policy veto"
        return LessonProposal(
            kind="pitfall",
            trigger_event="policy_decision",
            shape=lesson_shape("policy_decision", payload).key,
            fingerprint=lesson_fingerprint("policy_decision", canonical),
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
            shape=lesson_shape("debt_opened", payload).key,
            fingerprint=lesson_fingerprint("debt_opened", _canonical_payload("debt_opened", payload)),
            action_text=f"声称 {obligation}{target_part} 前必须存在对应工具回执"
            f"（{reason}）；不要凭空声称",
            counterfactual="回执机制改变或该声称允许用户接受债务",
            source_task=source_task,
        )
    if event_kind == "loop_detected":
        canonical = _canonical_payload("loop_detected", payload)
        # The runtime writes the signal as ``kind``; the canonical payload
        # falls back to it, so the wording and the fingerprint agree.
        signal = canonical["signal"] or "loop"
        reason = canonical["reason"] or "repeated call"
        return LessonProposal(
            kind="parameter",
            trigger_event="loop_detected",
            shape=lesson_shape("loop_detected", payload).key,
            fingerprint=lesson_fingerprint("loop_detected", canonical),
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
            shape=lesson_shape("user_approval", payload).key,
            fingerprint=lesson_fingerprint("user_approval", _canonical_payload("user_approval", payload)),
            action_text=f"{tool} 调用被用户拒绝（{reason}）——下次避免同类调用",
            counterfactual="用户后续显式允许了同类调用",
            source_task=source_task,
        )
    return None
