"""Deterministic decision-chain extractor (#370 follow-up, zero LLM).

Compaction folds events away; a model reading the compacted view must still
be able to answer "why did the task end up this way?".  Four kinds of events
carry exactly that answer — plan acceptance, control decisions (including
steering), and debt — so when those events are folded, their *reason* is
kept as ``why`` lines on the digest entry instead of being flattened into
"seq=… elided".

Pure function of the ledger summaries; never a model call, never a guess.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = ["decision_lines"]

_PLAN_KINDS = frozenset({"plan_accepted", "plan_updated", "plan_proposed"})
_CONTROL_KINDS = frozenset({"control_decision"})
_DEBT_KINDS = frozenset({"debt_opened"})


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def decision_lines(dropped: Sequence[Mapping[str, Any]]) -> list[str]:
    """One-line 'why' entries for the decision events being folded.

    Each line is a function of the summary item only: same ledger, same
    lines, in ledger order.
    """

    lines: list[str] = []
    for item in dropped:
        kind = str(item.get("kind") or "")
        if kind in _PLAN_KINDS:
            plan = item.get("plan")
            steps = (
                plan.get("steps") if isinstance(plan, Mapping) else ()
            ) or ()
            lines.append(f"{kind}: {len(steps)} step(s)")
        elif kind in _CONTROL_KINDS:
            why = _clip(item.get("reason") or "", 200)
            decision = item.get("decision") or item.get("kind") or "?"
            lines.append(f"control {decision}: " + (why or "no reason recorded"))
        elif kind in _DEBT_KINDS:
            target = _clip(item.get("target") or "", 80)
            why = _clip(item.get("reason") or "", 160)
            debt_kind = item.get("debt_kind") or item.get("kind") or "?"
            lines.append(
                f"debt {debt_kind} {target or '(no target)'}: "
                + (why or "no reason recorded")
            )
    return lines
