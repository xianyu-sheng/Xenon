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
3. **a lesson is a visible artifact** — a ``MemoryKind.LESSON`` record (there is
   no second store; ``/memory`` lists and archives it like any other memory),
   with an id, trigger, counterfactual, action and source task, plus
   ``LESSON_*`` events.

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
    "LESSON_STATES",
    "Lesson",
    "LessonShape",
    "canonical_failure_payload",
    "lesson_fingerprint",
    "lesson_shape",
]

DEFAULT_RETIRE_AFTER = 3
DEFAULT_STALE_DAYS = 30
# Guardrail 1 (which deterministic events may propose) is enforced by the
# explicit branches in ``lesson_proposer.propose_lesson``; there is no second
# allow-list to drift out of sync with them.


#: How a lesson's truthfulness advances.  ``hypothesis`` is a summary formed
#: from a few failures; ``active`` is a rule that has been applied and whose
#: failure did not recur; ``proven`` has survived enough applications.  A rule
#: that keeps recurring is ``retired`` instead -- see ``Lesson.retired``.
LESSON_STATES = ("hypothesis", "active", "proven")


def canonical_failure_payload(
    event_kind: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    """The deterministic subset a fingerprint and a shape are built from.

    The runtime proposes with a subset (kind/target/reason) and the post-task
    recurrence scan sees the *full* event payload (with obligation_id, stage,
    attempts…).  Both must compute the same identity, so the subset is the
    canonical shape for every event kind -- extra fields never change it.
    """

    if event_kind == "policy_decision":
        # The gate writes ``scope``, not ``capability``; without this fallback
        # every policy lesson had an empty subject, so two denials of the same
        # capability were two unrelated lessons and relevance could never
        # match them against a task's catalog.
        scope = payload.get("scope") or ()
        subject = payload.get("capability") or payload.get("tool") or ""
        if not subject and isinstance(scope, (list, tuple)) and scope:
            subject = scope[0]
        return {
            "kind": str(payload.get("kind") or ""),
            "capability": str(subject),
            "reason": str(payload.get("reason") or ""),
        }
    if event_kind == "debt_opened":
        return {
            "kind": str(payload.get("kind") or ""),
            "target": str(payload.get("target") or ""),
            "reason": str(payload.get("reason") or ""),
        }
    if event_kind == "loop_detected":
        # The runtime writes the signal as ``kind`` on the event; the proposer
        # is called with ``signal``.  Reading only ``signal`` made the
        # post-task recurrence scan rebuild ``loop_detected|`` for the very
        # event that proposed ``loop_detected|identical_call`` -- a loop
        # lesson could never recur, never retire, and its rule was confirmed
        # in the same task that looped.
        return {
            "signal": str(payload.get("signal") or payload.get("kind") or ""),
            "reason": str(payload.get("reason") or ""),
        }
    if event_kind == "user_approval":
        return {
            "approved": bool(payload.get("approved")),
            "tool": str(payload.get("tool") or ""),
            "reason": str(payload.get("reason") or ""),
        }
    return dict(payload)


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


@dataclass(frozen=True, slots=True)
class LessonShape:
    """The *structural* identity of a failure, without its exact payload.

    Two failures share a shape when they are the same kind of event on the same
    subject: two denied ``shell`` calls with different commands, or two debts
    about the same obligation.  The fingerprint still separates exact
    recurrences; the shape is what lets a *summary* (a rule) cover a family
    and what lets a task ask "does this lesson even concern me?" without a
    keyword list.  Subjects come from code-owned fields (capability, tool,
    obligation kind, loop signal), never from free prose.
    """

    event: str
    subject: str

    @property
    def key(self) -> str:
        return f"{self.event}|{self.subject}"

    @classmethod
    def parse(cls, key: str) -> "LessonShape":
        event, _, subject = str(key or "").partition("|")
        return cls(event=event, subject=subject)


def lesson_shape(event_kind: str, payload: Mapping[str, Any]) -> LessonShape:
    """Derive the shape a failure belongs to, from its canonical payload."""

    canonical = canonical_failure_payload(event_kind, payload)
    if event_kind == "policy_decision":
        return LessonShape(event_kind, str(canonical.get("capability") or ""))
    if event_kind == "debt_opened":
        return LessonShape(event_kind, str(canonical.get("kind") or ""))
    if event_kind == "loop_detected":
        return LessonShape(event_kind, str(canonical.get("signal") or ""))
    if event_kind == "user_approval":
        return LessonShape(event_kind, str(canonical.get("tool") or ""))
    return LessonShape(event_kind, "")


@dataclass(slots=True)
class Lesson:
    """One evidence-gated lesson."""

    id: str
    kind: str  # pitfall / parameter / preference / rule
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
    #: Structural family (``LessonShape.key``).  Empty on a lesson stored
    #: before shapes existed; such a lesson is carried universally.
    shape: str = ""
    #: ``hypothesis`` / ``active`` / ``proven`` (see ``LESSON_STATES``).  A
    #: plain pitfall starts ``active``; a summarized rule starts as a
    #: hypothesis and earns its state by surviving applications.
    state: str = "active"
    #: For a rule: the lesson ids it summarizes.  Empty for a primitive lesson.
    derived_from: tuple[str, ...] = ()

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
            "shape": self.shape,
            "state": self.state,
            "derived_from": list(self.derived_from),
        }

    @property
    def is_rule(self) -> bool:
        return self.kind == "rule"

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
                shape=str(data.get("shape") or ""),
                state=str(data.get("state") or "active"),
                derived_from=tuple(
                    str(item) for item in (data.get("derived_from") or ())
                ),
            )
        except (KeyError, TypeError, ValueError):
            # A malformed lesson is dropped, not guessed: applying a guessed
            # lesson could constrain future tasks for no reason.
            return None
