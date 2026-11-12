"""Lesson store: the durable half of self-evolution, on top of memory.

Design doc §13.2#9 settles this: lessons **are** memory.  There is no second
``lessons.yaml`` any more — a lesson is a ``MemoryKind.LESSON`` record, and
``/memory`` can list, edit and archive it like any other memory.

What this module adds is the one thing memory retrieval deliberately does not
do: **deterministic identity**.  Memory retrieval is scored and fuzzy, which is
right for "what did we decide about caching" and wrong for "this exact failure
happened again".  A lesson is matched by ``lesson_fingerprint`` — a hash of the
canonical part of the failure event — so the same ``policy_decision`` or
``debt_opened`` always finds the same lesson, with no model in the loop and a
replay that comes out identical.

Lifecycle (every step wrote an event since slice 2; only storage was missing):

    LESSON_PROPOSED  kernel, at the failure point   → ``store(proposal)``
    LESSON_STORED    this module, once              → memory record created
    LESSON_APPLIED   runner, when carried into a task → ``record_application``
    LESSON_RETIRED   runner, after recurrences      → ``record_recurrence``

Counters live on the memory record (``use_count`` for applications, a tag for
recurrences) so there is no parallel state to drift.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from xenon.kernel.lessons import (
    DEFAULT_RETIRE_AFTER,
    DEFAULT_STALE_DAYS,
    Lesson,
    lesson_fingerprint,
)
from xenon.memory.models import MemoryKind, MemoryRecord, MemoryScope, MemoryStatus

__all__ = [
    "LessonStore",
    "lesson_from_record",
    "record_from_lesson",
    "record_recurrences",
    "store_proposals",
]

#: The fingerprint is stored as a tag: memory has no column for it, and a tag
#: keeps it visible in the Markdown view a human reads.
_FP_TAG = "fp:"
#: Recurrences are counted in a tag for the same reason.
_RECURRENCE_TAG = "recurrences:"
#: Lessons are learned in a project and stay in it.
_SCOPE = MemoryScope.PROJECT_LOCAL


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _parse_iso(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def record_from_lesson(lesson: Lesson, existing: MemoryRecord | None = None) -> MemoryRecord:
    """Project one lesson onto the memory record that stores it.

    The fingerprint and the recurrence count ride as tags so a human reading
    ``lessons.md`` sees why two failures were considered the same.
    """

    tags = {
        tag
        for tag in (existing.tags if existing is not None else [])
        if not tag.startswith(_RECURRENCE_TAG)
    }
    if lesson.fingerprint:
        tags.add(f"{_FP_TAG}{lesson.fingerprint}")
    if lesson.kind:
        tags.add(f"lesson:{lesson.kind}")
    tags.add(f"{_RECURRENCE_TAG}{lesson.recurrences}")

    return MemoryRecord(
        content=lesson.action_text,
        scope=_SCOPE,
        kind=MemoryKind.LESSON,
        id=lesson.id,
        tags=sorted(tags),
        created_at=(
            existing.created_at if existing is not None else _iso(lesson.created_at)
        ),
        updated_at=_iso(time.time()),
        last_used_at=(
            _iso(lesson.last_applied_at) if lesson.last_applied_at else None
        ),
        use_count=lesson.applied,
        importance=0.6,
        confidence=0.7,
        source=f"lesson:{lesson.trigger_event}" if lesson.trigger_event else "lesson",
        evidence=lesson.source_task or None,
        status=MemoryStatus.ARCHIVED if lesson.retired else MemoryStatus.ACTIVE,
    )


def lesson_from_record(record: MemoryRecord) -> Lesson:
    """Rebuild the kernel's ``Lesson`` from a stored record."""

    fingerprint = ""
    kind = ""
    recurrences = 0
    for tag in record.tags:
        if tag.startswith(_FP_TAG):
            fingerprint = tag[len(_FP_TAG):]
        elif tag.startswith("lesson:"):
            kind = tag[len("lesson:"):]
        elif tag.startswith(_RECURRENCE_TAG):
            try:
                recurrences = int(tag[len(_RECURRENCE_TAG):])
            except ValueError:
                recurrences = 0
    trigger = record.source.split(":", 1)[1] if ":" in record.source else ""

    return Lesson(
        id=record.id,
        kind=kind or "pitfall",
        trigger_event=trigger,
        fingerprint=fingerprint,
        action_text=record.content,
        source_task=record.evidence or "",
        counterfactual="",
        applied=record.use_count,
        recurrences=recurrences,
        retired=record.status is not MemoryStatus.ACTIVE,
        created_at=_parse_iso(record.created_at),
        last_applied_at=_parse_iso(record.last_used_at),
    )


@dataclass
class LessonStore:
    """Deterministic lesson matching over the single memory store.

    ``service`` is a ``MemoryService``.  The store does no filesystem work of
    its own: there is exactly one place lessons are persisted, and it is the
    same place every other memory lives.
    """

    service: Any
    _cache: list[Lesson] | None = field(default=None, repr=False)

    # ── reading ────────────────────────────────────────────────────

    def invalidate(self) -> None:
        self._cache = None

    def _lessons(self) -> list[Lesson]:
        if self._cache is not None:
            return self._cache
        records = [
            record
            for record in self.service.list_records(include_archived=True)
            if record.kind is MemoryKind.LESSON
        ]
        self._cache = [lesson_from_record(record) for record in records]
        return self._cache

    def __len__(self) -> int:
        return len(self._lessons())

    def __iter__(self) -> Iterable[Lesson]:
        return iter(tuple(self._lessons()))

    def active(self) -> tuple[Lesson, ...]:
        return tuple(lesson for lesson in self._lessons() if not lesson.retired)

    def match(
        self,
        event_kind: str,
        payload: Mapping[str, Any],
        *,
        clock: Any = time.time,
    ) -> Lesson | None:
        """The active lesson for this exact failure, if any.

        Deterministic: same event shape, same fingerprint, same lesson.  A
        retired or stale lesson never applies — that is guardrail 2.
        """

        fingerprint = lesson_fingerprint(event_kind, payload)
        now = clock()
        for lesson in self._lessons():
            if lesson.retired or lesson.fingerprint != fingerprint:
                continue
            if lesson.last_applied_at and now - lesson.last_applied_at > (
                DEFAULT_STALE_DAYS * 86400
            ):
                continue
            return lesson
        return None

    def match_for_task(self, limit: int = 3) -> tuple[Lesson, ...]:
        """The lessons a new task should carry, bounded.

        Injecting every lesson would dilute the constraints section, so only a
        bounded set of recently applied ones is offered.  ``parameter`` lessons
        adjust a profile — a build-time decision the caller makes — so they are
        not pushed into a task.
        """

        if int(limit) <= 0:
            return ()
        candidates = sorted(
            (
                lesson
                for lesson in self._lessons()
                if not lesson.retired and lesson.kind != "parameter"
            ),
            key=lambda item: item.last_applied_at,
            reverse=True,
        )
        return tuple(candidates[: int(limit)])

    def rows(self) -> list[dict[str, Any]]:
        return [
            {
                "id": lesson.id,
                "kind": lesson.kind,
                "trigger": lesson.trigger_event,
                "applied": lesson.applied,
                "recurrences": lesson.recurrences,
                "retired": lesson.retired,
                "text": lesson.action_text,
            }
            for lesson in self._lessons()
        ]

    # ── writing ────────────────────────────────────────────────────

    def store(self, proposal: Any) -> Lesson | None:
        """Persist one proposal, or return the lesson already carrying it.

        Idempotent by fingerprint: a failure that repeats does not create a
        second lesson, it bumps the one already learned.
        """

        fingerprint = str(getattr(proposal, "fingerprint", "") or "")
        text = str(getattr(proposal, "action_text", "") or "")
        if not fingerprint or not text:
            return None
        for lesson in self._lessons():
            if lesson.fingerprint == fingerprint:
                return lesson

        lesson = Lesson(
            id=_lesson_id(fingerprint),
            kind=str(getattr(proposal, "kind", "") or "pitfall"),
            trigger_event=str(getattr(proposal, "trigger_event", "") or ""),
            fingerprint=fingerprint,
            action_text=text,
            source_task=str(getattr(proposal, "source_task", "") or ""),
            counterfactual=str(getattr(proposal, "counterfactual", "") or ""),
        )
        self.add(lesson)
        return lesson

    def add(self, lesson: Lesson) -> Lesson:
        """Persist a fully-formed lesson, replacing the record with its id."""

        record = record_from_lesson(lesson, self._existing(lesson.id))
        self._write(record)
        self.invalidate()
        return lesson

    def record_application(self, lesson_id: str, *, clock: Any = time.time) -> bool:
        lesson = self._find(lesson_id)
        if lesson is None:
            return False
        lesson.applied += 1
        lesson.last_applied_at = clock()
        self.add(lesson)
        return True

    def record_recurrence(
        self, lesson_id: str, *, retire_after: int = DEFAULT_RETIRE_AFTER
    ) -> bool:
        """Count that the failure happened again; retire once it keeps happening.

        Returns whether this call retired the lesson.
        """

        lesson = self._find(lesson_id)
        if lesson is None or lesson.retired:
            return False
        lesson.recurrences += 1
        retire = lesson.recurrences >= max(1, int(retire_after))
        if retire:
            lesson.retired = True
        self.add(lesson)
        return retire

    def retire(self, lesson_id: str) -> bool:
        lesson = self._find(lesson_id)
        if lesson is None or lesson.retired:
            return False
        lesson.retired = True
        self.add(lesson)
        return True

    # ── internals ──────────────────────────────────────────────────

    def _find(self, lesson_id: str) -> Lesson | None:
        for lesson in self._lessons():
            if lesson.id == lesson_id:
                return lesson
        return None

    def _existing(self, memory_id: str) -> MemoryRecord | None:
        for record in self.service.list_records(include_archived=True):
            if record.id == memory_id:
                return record
        return None

    def _write(self, record: MemoryRecord) -> None:
        """Upsert one record through the memory backend.

        ``mutate_records`` is the backend's own write path — using it means a
        lesson is persisted exactly like every other memory (same atomic write,
        same Markdown projection), instead of a second file format.
        """

        def mutate(records: list[MemoryRecord]) -> None:
            for index, existing in enumerate(records):
                if existing.id == record.id:
                    records[index] = record
                    return
            records.append(record)

        backend = self.service.registry.get(_SCOPE)
        backend.mutate_records(mutate, render=True)


def _lesson_id(fingerprint: str) -> str:
    """A stable id from the fingerprint, so a replay produces the same id."""

    return f"lsn-{fingerprint[:16]}"


def store_proposals(ledger: Any, store: LessonStore) -> list[str]:
    """Persist the lessons the kernel proposed during this task.

    The kernel writes ``LESSON_PROPOSED`` at the failure point and stops there
    — it has no filesystem.  This is the step that makes the proposal durable,
    and it is why self-evolution had a ledger full of proposals and no stored
    lesson at all.

    Returns the ids that were newly created (a repeat proposal returns the
    lesson that already exists and is not reported again).
    """

    created: list[str] = []
    if not isinstance(store, LessonStore):
        return created
    try:
        events = ledger.read()
    except Exception:  # noqa: BLE001 — learning is advisory
        return created

    for event in events:
        if getattr(getattr(event, "kind", None), "value", "") != "lesson_proposed":
            continue
        payload = dict(getattr(event, "payload", {}) or {})
        proposal = _Proposal(
            kind=str(payload.get("kind") or ""),
            trigger_event=str(payload.get("trigger_event") or ""),
            fingerprint=str(payload.get("fingerprint") or ""),
            action_text=str(payload.get("action_text") or ""),
            source_task=str(payload.get("source_task") or ""),
            counterfactual=str(payload.get("counterfactual") or ""),
        )
        if not proposal.fingerprint or not proposal.action_text:
            continue
        before = {lesson.fingerprint for lesson in store}
        lesson = store.store(proposal)
        if lesson is not None and proposal.fingerprint not in before:
            created.append(lesson.id)
    return created


@dataclass(frozen=True, slots=True)
class _Proposal:
    """The payload shape a stored proposal has in the ledger."""

    kind: str
    trigger_event: str
    fingerprint: str
    action_text: str
    source_task: str = ""
    counterfactual: str = ""


def record_recurrences(
    ledger: Any, store: LessonStore, *, retire_after: int = DEFAULT_RETIRE_AFTER
) -> list[str]:
    """After a task, count applied lessons whose failure happened again.

    A recurrence is the same deterministic failure (same fingerprint) observed
    after a lesson was applied.  Lessons past the threshold are retired here;
    the caller writes ``LESSON_RETIRED``.

    Reads only what the runtime already recorded — no new inference.
    """

    retired: list[str] = []
    if not isinstance(store, LessonStore):
        return retired
    try:
        events = ledger.read()
    except Exception:  # noqa: BLE001 — counting is advisory
        return retired

    applied = {
        str((getattr(event, "payload", {}) or {}).get("lesson_id") or "")
        for event in events
        if getattr(getattr(event, "kind", None), "value", "") == "lesson_applied"
    }
    applied.discard("")
    if not applied:
        return retired

    for event in events:
        kind_value = str(getattr(getattr(event, "kind", None), "value", "") or "")
        if kind_value not in {
            "debt_opened",
            "loop_detected",
            "policy_decision",
            "user_approval",
        }:
            continue
        # Reuse the proposer so the fingerprint here matches the one computed at
        # proposal time (the full payload carries ids the subset must ignore).
        try:
            from xenon.kernel.lesson_proposer import propose_lesson

            proposal = propose_lesson(kind_value, dict(getattr(event, "payload", {}) or {}))
        except Exception:  # noqa: BLE001 — counting is advisory
            continue
        if proposal is None:
            continue
        for lesson in store:
            if lesson.retired or lesson.fingerprint != proposal.fingerprint:
                continue
            if lesson.id not in applied:
                continue
            if store.record_recurrence(lesson.id, retire_after=retire_after):
                retired.append(lesson.id)
    return retired
