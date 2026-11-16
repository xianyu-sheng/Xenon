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

import hashlib
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from xenon.kernel.lessons import (
    DEFAULT_RETIRE_AFTER,
    DEFAULT_STALE_DAYS,
    Lesson,
    LessonShape,
    lesson_fingerprint,
    lesson_shape,
)
from xenon.memory.models import MemoryKind, MemoryRecord, MemoryScope, MemoryStatus

__all__ = [
    "LessonStore",
    "applied_lesson_ids",
    "design_debt_payload",
    "failure_shapes",
    "lesson_from_record",
    "record_from_lesson",
    "record_recurrences",
    "store_proposals",
    "summarize_and_confirm",
]

#: The fingerprint is stored as a tag: memory has no column for it, and a tag
#: keeps it visible in the Markdown view a human reads.
_FP_TAG = "fp:"
#: Recurrences are counted in a tag for the same reason.
_RECURRENCE_TAG = "recurrences:"
#: The structural family (``event|subject``) and the rule state ride as tags
#: too, so the Markdown view a human reads shows why two failures were treated
#: as one family and whether a rule is still a hypothesis.
_SHAPE_TAG = "shape:"
_STATE_TAG = "state:"
_FROM_TAG = "from:"
#: The falsifiability condition travels with the record too.  Keeping it only
#: in the proposal event meant the stored lesson could not answer "when does
#: this stop being true?".
_CF_TAG = "cf:"
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
        if not tag.startswith(
            (_RECURRENCE_TAG, _SHAPE_TAG, _STATE_TAG, _FROM_TAG, _CF_TAG)
        )
    }
    if lesson.fingerprint:
        tags.add(f"{_FP_TAG}{lesson.fingerprint}")
    if lesson.kind:
        tags.add(f"lesson:{lesson.kind}")
    tags.add(f"{_RECURRENCE_TAG}{lesson.recurrences}")
    if lesson.shape:
        tags.add(f"{_SHAPE_TAG}{lesson.shape}")
    tags.add(f"{_STATE_TAG}{lesson.state or 'active'}")
    if lesson.derived_from:
        tags.add(f"{_FROM_TAG}{','.join(lesson.derived_from)}")
    if lesson.counterfactual:
        tags.add(f"{_CF_TAG}{lesson.counterfactual}")

    owned = dict(
        content=lesson.action_text,
        scope=_SCOPE,
        kind=MemoryKind.LESSON,
        id=lesson.id,
        tags=sorted(tags),
        updated_at=_iso(time.time()),
        last_used_at=(
            _iso(lesson.last_applied_at) if lesson.last_applied_at else None
        ),
        use_count=lesson.applied,
        source=f"lesson:{lesson.trigger_event}" if lesson.trigger_event else "lesson",
        evidence=lesson.source_task or None,
        status=MemoryStatus.ARCHIVED if lesson.retired else MemoryStatus.ACTIVE,
    )
    if existing is not None:
        # Everything the lesson does not own stays as the user left it:
        # ``/memory pin``, expiry, supersession, retrieval counters.  The
        # previous rebuild-from-defaults silently un-pinned a lesson every
        # time a task applied it.
        return replace(existing, checksum="", **owned)
    return MemoryRecord(
        created_at=_iso(lesson.created_at),
        importance=0.6,
        confidence=0.7,
        **owned,
    )


def lesson_from_record(record: MemoryRecord) -> Lesson:
    """Rebuild the kernel's ``Lesson`` from a stored record."""

    fingerprint = ""
    kind = ""
    recurrences = 0
    shape = ""
    state = "active"
    counterfactual = ""
    derived_from: tuple[str, ...] = ()
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
        elif tag.startswith(_SHAPE_TAG):
            shape = tag[len(_SHAPE_TAG):]
        elif tag.startswith(_STATE_TAG):
            state = tag[len(_STATE_TAG):] or "active"
        elif tag.startswith(_FROM_TAG):
            derived_from = tuple(
                item for item in tag[len(_FROM_TAG):].split(",") if item
            )
        elif tag.startswith(_CF_TAG):
            counterfactual = tag[len(_CF_TAG):]
    trigger = record.source.split(":", 1)[1] if ":" in record.source else ""

    return Lesson(
        id=record.id,
        kind=kind or "pitfall",
        trigger_event=trigger,
        fingerprint=fingerprint,
        action_text=record.content,
        source_task=record.evidence or "",
        counterfactual=counterfactual,
        applied=record.use_count,
        recurrences=recurrences,
        retired=record.status is not MemoryStatus.ACTIVE,
        created_at=_parse_iso(record.created_at),
        last_applied_at=_parse_iso(record.last_used_at),
        shape=shape,
        state=state,
        derived_from=derived_from,
    )


@dataclass
class LessonStore:
    """Deterministic lesson matching over the single memory store.

    ``service`` is a ``MemoryService``.  The store does no filesystem work of
    its own: there is exactly one place lessons are persisted, and it is the
    same place every other memory lives.
    """

    service: Any

    # ── reading ────────────────────────────────────────────────────

    def _lessons(self) -> list[Lesson]:
        # Read through to the backend on every call.  A process-local cache
        # went stale as soon as ``/memory archive`` (or a pin/edit) mutated the
        # record behind it; the next task then applied an archived lesson and
        # rewrote it as ACTIVE.  Lesson sets are small; correctness wins.
        #
        # Two deliberate restrictions:
        # * PROJECT_LOCAL only -- writes go there, so reading USER /
        #   PROJECT_SHARED / SESSION would let a lesson learned elsewhere ride
        #   into this project (or an id collision split updates across two
        #   backends);
        # * tolerate a malformed scope -- one bad metadata file must not disable
        #   every lesson in the project.
        records = [
            record
            for record in self.service.list_records(
                scope=_SCOPE, include_archived=True, tolerate_errors=True
            )
            if record.kind is MemoryKind.LESSON
        ]
        return [lesson_from_record(record) for record in records]

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

    def match_for_task(
        self,
        limit: int = 3,
        *,
        available: Iterable[str] = (),
        clock: Any = time.time,
    ) -> tuple[Lesson, ...]:
        """The lessons a new task should carry, bounded and relevant.

        The old rule was "the three most recently applied lessons", which made
        a shell-permission pitfall ride into a prose task and diluted the
        constraint section.  Relevance is structural, never a keyword list:

        * a ``policy_decision`` / ``user_approval`` lesson concerns this task
          only when its subject (capability or tool) exists in this task's
          catalog;
        * ``debt_opened`` / ``loop_detected`` are claim/trajectory failures
          that any task can hit, so they stay universal;
        * when a ``rule`` summarizes a shape, the rule is carried and its
          member lessons are not (the summary replaces the enumeration);
        * ``parameter`` lessons are carried as advice instead of being stored
          and ignored.  This slice does not mutate budgets on its own: a rule
          may only tighten what the caller already allowed.
        """

        bound = int(limit)
        if bound <= 0:
            return ()
        subjects = {str(item) for item in available if str(item)}
        now = clock()
        every = self._lessons()
        lessons = [lesson for lesson in every if not lesson.retired]
        # A primitive lesson is represented by its family's rule -- including
        # a *retired* rule.  Re-enumerating the members of a failed summary
        # would bring back the exact guidance that just failed; the design
        # debt, not another prompt line, is the answer.
        rule_shapes = {lesson.shape for lesson in every if lesson.is_rule}
        scored: list[tuple[int, float, str, Lesson]] = []
        for lesson in lessons:
            if lesson.last_applied_at and now - lesson.last_applied_at > (
                DEFAULT_STALE_DAYS * 86400
            ):
                # Guardrail 2's staleness rule; ``match()`` had it, the
                # production path did not.
                continue
            if lesson.is_rule or lesson.shape not in rule_shapes:
                relevance = self._relevance(lesson, subjects)
                if relevance is None:
                    continue
                scored.append(
                    (relevance, lesson.last_applied_at, lesson.id, lesson)
                )
        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return tuple(lesson for _r, _a, _i, lesson in scored[:bound])

    @staticmethod
    def _relevance(lesson: Lesson, subjects: set[str]) -> int | None:
        """2 = exact subject, 1 = universal, ``None`` = irrelevant to this task."""

        if not lesson.shape:
            return 1
        shape = LessonShape.parse(lesson.shape)
        if shape.event in {"policy_decision", "user_approval"}:
            if not subjects:
                # No catalog information: fail open, never silently drop a
                # safety lesson.
                return 1
            if shape.subject in subjects:
                return 2
            return None
        return 1

    def summarize(self, *, min_members: int = 2) -> list[Lesson]:
        """Fold repeated failures of one shape into a deterministic rule.

        This is the "总结" half of self-evolution: a rule is a *summary* of a
        family, not a new source of truth.  It is derived only from lessons
        that already passed guardrail 1, its text is a template over the shape,
        and every underlying lesson id is kept in ``derived_from`` so the
        summary can be audited or withdrawn.  A rule starts as a
        ``hypothesis`` and earns ``active``/``proven`` only by surviving
        applications (``record_confirmation``) or dies as a recurrence
        (``record_recurrence``).
        """

        threshold = max(2, int(min_members))
        groups: dict[str, list[Lesson]] = {}
        for lesson in self._lessons():
            if lesson.retired or lesson.is_rule or not lesson.shape:
                continue
            groups.setdefault(lesson.shape, []).append(lesson)

        created: list[Lesson] = []
        for shape, members in sorted(groups.items()):
            if len(members) < threshold:
                continue
            members = sorted(members, key=lambda member: member.id)
            ids = tuple(member.id for member in members)
            rule_id = _rule_id(shape)
            existing = self._find(rule_id)
            if existing is not None:
                if existing.retired:
                    # The family recurred despite its rule; the answer is a
                    # design change, not another summary.  Re-creating it here
                    # would resurrect the exact wording that just failed.
                    continue
                if existing.derived_from == ids:
                    continue
                updated = replace(
                    existing,
                    action_text=_rule_text(shape, len(ids)),
                    derived_from=ids,
                )
                self.add(updated)
                continue
            rule = Lesson(
                id=rule_id,
                kind="rule",
                trigger_event=LessonShape.parse(shape).event,
                fingerprint=lesson_fingerprint("rule", {"shape": shape}),
                action_text=_rule_text(shape, len(ids)),
                source_task=members[0].source_task,
                counterfactual="同类失败在应用该规则后仍复发达阈值",
                shape=shape,
                state="hypothesis",
                derived_from=ids,
            )
            self.add(rule)
            created.append(rule)
        return created

    def record_confirmation(self, lesson_id: str) -> str | None:
        """A rule was applied and its failure did not recur.

        Returns the new state when it advanced, else ``None``.  Thresholds are
        deliberately small and code-owned: one quiet application makes a
        hypothesis ``active``, five make it ``proven``.
        """

        from xenon.kernel.lessons import LESSON_STATES

        lesson = self._find(lesson_id)
        if lesson is None or lesson.retired or not lesson.is_rule:
            return None
        before = lesson.state or "hypothesis"
        if before not in LESSON_STATES:
            # A hand-edited record must not crash the learning pass.
            before = "hypothesis"
        if lesson.applied >= 5:
            earned = "proven"
        elif lesson.applied >= 2:
            earned = "active"
        else:
            earned = "hypothesis"
        # State only advances.  Deriving it from lifetime ``applied`` alone
        # let an external counter reset move ``proven`` back to ``active``.
        after = max(before, earned, key=LESSON_STATES.index)
        if after == before:
            return None
        lesson.state = after
        self.add(lesson)
        return after

    def get(self, lesson_id: str) -> Lesson | None:
        """One lesson by id (``None`` when it does not exist)."""

        return self._find(lesson_id)

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
                "shape": lesson.shape,
                "state": "retired" if lesson.retired else lesson.state,
                "derived_from": list(lesson.derived_from),
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
            shape=str(getattr(proposal, "shape", "") or ""),
        )
        self.add(lesson)
        return lesson

    def add(self, lesson: Lesson) -> Lesson:
        """Persist a fully-formed lesson, replacing the record with its id."""

        record = record_from_lesson(lesson, self._existing(lesson.id))
        self._write(record)
        return lesson

    def record_application(self, lesson_id: str, *, clock: Any = time.time) -> bool:
        lesson = self._find(lesson_id)
        if lesson is None or lesson.retired:
            # An archived lesson is archived: applying it must not rewrite the
            # record back to ACTIVE.
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
        for record in self.service.list_records(
            scope=_SCOPE, include_archived=True, tolerate_errors=True
        ):
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


def _rule_id(shape: str) -> str:
    """One rule per structural family, stable across replays."""

    digest = hashlib.sha256(str(shape).encode("utf-8")).hexdigest()[:16]
    return f"rule-{digest}"


#: Deterministic summary templates.  They name the structural subject (a
#: capability, tool, obligation kind or loop signal) and the number of
#: observed failures -- no model prose, no invented cause.
_RULE_TEMPLATES: dict[str, str] = {
    "policy_decision": (
        "{subject} 在同类任务中已被策略拒绝 {count} 次；"
        "同类操作前先确认权限，或改走已允许的路径"
    ),
    "debt_opened": (
        "声称 {subject} 前必须存在对应回执（同类失败已出现 {count} 次）；"
        "不要凭空声称"
    ),
    "loop_detected": (
        "{subject} 循环信号已出现 {count} 次；重复调用前换方法或先读取状态"
    ),
    "user_approval": (
        "{subject} 被用户拒绝 {count} 次；同类调用先询问再执行"
    ),
}


def _rule_text(shape: str, count: int) -> str:
    parsed = LessonShape.parse(shape)
    template = _RULE_TEMPLATES.get(
        parsed.event,
        "同类失败（{event} / {subject}）已出现 {count} 次；执行前先确认前提",
    )
    return template.format(
        event=parsed.event, subject=parsed.subject or "?", count=int(count)
    )


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
            shape=str(payload.get("shape") or ""),
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
    shape: str = ""


def record_recurrences(
    ledger: Any, store: LessonStore, *, retire_after: int = DEFAULT_RETIRE_AFTER
) -> list[str]:
    """After a task, count applied lessons whose failure happened again.

    A recurrence is the same deterministic failure (same fingerprint) observed
    **after the most recent application** of that lesson.  The window starts at
    the latest ``LESSON_APPLIED`` sequence, so the failure that taught the
    lesson -- or a plan-phase failure already judged before the approved
    resume -- is not counted a second time.  Lessons past the threshold are
    retired here; the caller writes ``LESSON_RETIRED``.

    Reads only what the runtime already recorded — no new inference.
    """

    retired: list[str] = []
    if not isinstance(store, LessonStore):
        return retired
    try:
        events = ledger.read()
    except Exception:  # noqa: BLE001 — counting is advisory
        return retired

    # A recurrence is a failure *after* the lesson was carried.  The ledger
    # holds both the plan phase and the resumed execution of one user task, and
    # the plan-phase failures were already judged when the plan was proposed;
    # scanning by membership alone counted them again on resume (and could
    # retire a lesson on its first day).  Use the latest application sequence
    # as the window's start.
    applied_at: dict[str, int] = {}
    for event in events:
        if str(getattr(getattr(event, "kind", None), "value", "") or "") != (
            "lesson_applied"
        ):
            continue
        lesson_id = str((getattr(event, "payload", {}) or {}).get("lesson_id") or "")
        if lesson_id:
            applied_at[lesson_id] = max(
                applied_at.get(lesson_id, 0), int(getattr(event, "seq", 0) or 0)
            )
    if not applied_at:
        return retired
    # One snapshot; ``record_recurrence`` re-reads by id when it mutates.
    lessons = tuple(store)

    for event in events:
        kind_value = str(getattr(getattr(event, "kind", None), "value", "") or "")
        if kind_value not in {
            "debt_opened",
            "loop_detected",
            "policy_decision",
            "user_approval",
        }:
            continue
        seq = int(getattr(event, "seq", 0) or 0)
        # Reuse the proposer so the fingerprint here matches the one computed at
        # proposal time (the full payload carries ids the subset must ignore).
        try:
            from xenon.kernel.lesson_proposer import propose_lesson

            proposal = propose_lesson(kind_value, dict(getattr(event, "payload", {}) or {}))
        except Exception:  # noqa: BLE001 — counting is advisory
            continue
        if proposal is None:
            continue
        # A rule is not matched by fingerprint (its own fingerprint is over the
        # shape), so it recurs whenever *any* failure of its family recurs.
        shape_key = lesson_shape(
            kind_value, dict(getattr(event, "payload", {}) or {})
        ).key
        for lesson in lessons:
            if lesson.retired:
                continue
            start = applied_at.get(lesson.id)
            if start is None or seq <= start:
                continue
            matches = lesson.fingerprint == proposal.fingerprint or (
                lesson.is_rule and lesson.shape == shape_key
            )
            if not matches:
                continue
            if store.record_recurrence(lesson.id, retire_after=retire_after):
                retired.append(lesson.id)
    return retired


def applied_lesson_ids(ledger: Any) -> set[str]:
    """Lesson ids this task recorded as applied (empty on an unreadable ledger)."""

    try:
        events = ledger.read()
    except Exception:  # noqa: BLE001 — learning is advisory
        return set()
    applied = {
        str((getattr(event, "payload", {}) or {}).get("lesson_id") or "")
        for event in events
        if getattr(getattr(event, "kind", None), "value", "") == "lesson_applied"
    }
    applied.discard("")
    return applied


def failure_shapes(ledger: Any) -> dict[str, int]:
    """Structural shapes of the harness-judged failures in this ledger.

    Deterministic: built from the same canonical payloads the proposer uses,
    never from prose.  The count is the number of events of that shape.
    """

    shapes: dict[str, int] = {}
    try:
        events = ledger.read()
    except Exception:  # noqa: BLE001
        return shapes
    for event in events:
        kind_value = str(getattr(getattr(event, "kind", None), "value", "") or "")
        if kind_value not in {
            "policy_decision",
            "debt_opened",
            "loop_detected",
            "user_approval",
        }:
            continue
        # Reuse the proposer: a *successful* approval or a non-veto policy
        # decision is not a failure family, and the proposer already encodes
        # that judgment (guardrail 1).  Counting them here would suppress a
        # well-earned rule confirmation whenever the tool was approved again.
        try:
            from xenon.kernel.lesson_proposer import propose_lesson

            proposal = propose_lesson(
                kind_value, dict(getattr(event, "payload", {}) or {})
            )
        except Exception:  # noqa: BLE001 — learning is advisory
            continue
        if proposal is None or not getattr(proposal, "shape", ""):
            continue
        shape = str(proposal.shape)
        shapes[shape] = shapes.get(shape, 0) + 1
    return shapes


def summarize_and_confirm(
    ledger: Any, store: LessonStore, *, min_members: int = 2
) -> tuple[list[Lesson], list[tuple[str, str]]]:
    """Fold this task's failures into rules and confirm the rules that held.

    Two deterministic judgments, both computed from the ledger the runtime
    already wrote:

    * **summarize** — a family with at least ``min_members`` stored failures
      becomes one rule (``LessonStore.summarize``), so the next task carries
      the summary instead of the enumeration;
    * **confirm** — a rule that this task *applied* and whose family did not
      fail again earns state (``hypothesis -> active -> proven``).

    Returns ``(created_rules, [(lesson_id, new_state), ...])``.
    """

    created = store.summarize(min_members=min_members)
    shapes = failure_shapes(ledger)
    applied = applied_lesson_ids(ledger)
    advanced: list[tuple[str, str]] = []
    for lesson in tuple(store):
        if lesson.retired or not lesson.is_rule or lesson.id not in applied:
            continue
        if lesson.shape in shapes:
            # It recurred; ``record_recurrences`` owns that judgment.
            continue
        state = store.record_confirmation(lesson.id)
        if state:
            advanced.append((lesson.id, state))
    return created, advanced


def design_debt_payload(store: LessonStore, lesson_id: str) -> dict[str, Any] | None:
    """The design-debt record for a lesson that recurrence retired.

    A lesson that was applied and still recurred is evidence that *guidance*
    is not the missing mechanism.  The payload names the family and the
    mechanism question, so the debt is actionable without reading the prose.
    """

    get = getattr(store, "get", None)
    if not callable(get):
        return None
    lesson = get(lesson_id)
    if lesson is None:
        return None
    shape = LessonShape.parse(lesson.shape)
    return {
        "lesson_id": lesson.id,
        "shape": lesson.shape,
        "event": shape.event,
        "subject": shape.subject,
        "applied": lesson.applied,
        "recurrences": lesson.recurrences,
        "reason": "lesson_retired_after_recurrence",
        "next_action": (
            "同类失败在应用该教训后仍复发；需要机制修复"
            "（工具设计 / 策略 / 验证），而不是再加一条提示"
        ),
    }
