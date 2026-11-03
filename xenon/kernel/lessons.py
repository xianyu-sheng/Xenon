"""Evidence-gated lessons: learn from harness-judged failures, nothing else.

Design: ``docs/design/self-evolution.md`` (design-first, this module is slice 1).

The three guardrails live here in code:

1. **only harness-judged failures become lessons** — a lesson is *proposed* by
   deterministic events (blocked / debt_opened / loop_detected / denied
   approval), never by the model's prose;
2. **a lesson is falsifiable** — it carries a deterministic failure fingerprint
   (event kind + canonical payload hash) and a recurrence counter; after the
   threshold of recurrences with no improvement it is *retired* (recorded, not
   deleted);
3. **a lesson is a visible artifact** — ``~/.xenon/lessons.yaml`` with an id,
   trigger, counterfactual, action and source task; ``LESSON_*`` events; the
   per-repo ``PITFALLS.md`` projection lives in a later slice.

A lesson never rewrites a running task: it is applied when the *next* task with
the same fingerprint is built, as an extra constraint (pitfall), a profile
tweak (parameter) or a preference.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "DEFAULT_RETIRE_AFTER",
    "DEFAULT_STALE_DAYS",
    "Lesson",
    "lesson_fingerprint",
]

DEFAULT_RETIRE_AFTER = 3
DEFAULT_STALE_DAYS = 30
# Which deterministic events may *propose* a lesson (guardrail 1).
PROPOSING_EVENTS = frozenset(
    {
        "policy_decision",  # VETO (blocked)
        "debt_opened",
        "loop_detected",
        "user_approval",  # approved: false
    }
)


def lesson_fingerprint(event_kind: str, payload: Mapping[str, Any]) -> str:
    """Deterministic failure fingerprint: event kind + canonical payload hash.

    Not a text similarity: the same deterministic failure in the same shape is
    the same failure.  ``payload`` is canonically serialized, so field order
    never changes the fingerprint.
    """

    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(
        f"{event_kind}|{canonical}".encode("utf-8")
    ).hexdigest()[:24]


@dataclass(slots=True)
class Lesson:
    """One evidence-gated lesson."""

    id: str
    kind: str  # pitfall / parameter / preference
    trigger_event: str
    fingerprint: str
    action_text: str
    source_task: str = ""
    counterfactual: str = ""
    applied: int = 0
    recurrences: int = 0
    retired: bool = False
    created_at: float = field(default_factory=time.time)
    last_applied_at: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "trigger_event": self.trigger_event,
            "fingerprint": self.fingerprint,
            "action_text": self.action_text,
            "source_task": self.source_task,
            "counterfactual": self.counterfactual,
            "applied": self.applied,
            "recurrences": self.recurrences,
            "retired": self.retired,
            "created_at": self.created_at,
            "last_applied_at": self.last_applied_at,
        }

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> "Lesson | None":
        try:
            return cls(
                id=str(data["id"]),
                kind=str(data["kind"]),
                trigger_event=str(data["trigger_event"]),
                fingerprint=str(data["fingerprint"]),
                action_text=str(data["action_text"]),
                source_task=str(data.get("source_task") or ""),
                counterfactual=str(data.get("counterfactual") or ""),
                applied=int(data.get("applied") or 0),
                recurrences=int(data.get("recurrences") or 0),
                retired=bool(data.get("retired") or False),
                created_at=float(data.get("created_at") or time.time()),
                last_applied_at=float(data.get("last_applied_at") or 0.0),
            )
        except (KeyError, TypeError, ValueError):
            # A malformed lesson is dropped, not guessed: applying a guessed
            # lesson could constrain future tasks for no reason.
            return None


