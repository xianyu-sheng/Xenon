"""Lesson store: the memory-backed half of self-evolution.

Covers the rules the rest of the chain relies on (docs/design/self-evolution.md):

* deterministic fingerprint identity (``xenon/kernel/lessons.py``);
* the lesson ↔ memory projection (``record_from_lesson`` / ``lesson_from_record``);
* idempotent storage, application and recurrence counters, retirement;
* ``store_proposals`` / ``record_recurrences`` over a ledger;
* the runner's end-to-end chain: proposal -> stored -> applied -> retired.

There is no ``lessons.yaml`` any more: a lesson is a ``MemoryKind.LESSON`` record
(design decision: lessons **are** memory), so every store here is built on a real
``MemoryService`` backed by a temp project directory.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from xenon.harness.lessons import (
    LessonStore,
    lesson_from_record,
    record_from_lesson,
)
from xenon.kernel.lesson_proposer import propose_lesson
from xenon.kernel.lessons import (
    Lesson,
    lesson_fingerprint,
)
from xenon.ledger.events import EventKind, EventSource

# ── fixtures ──────────────────────────────────────────────────────


def _service(tmp_path: Path) -> Any:
    from xenon.memory import MemoryBackendRegistry, MemoryService

    return MemoryService(MemoryBackendRegistry(tmp_path))


def _store(tmp_path: Path) -> LessonStore:
    return LessonStore(_service(tmp_path))


def _lesson(**kwargs: Any) -> Lesson:
    base = dict(
        id="L-0001",
        kind="pitfall",
        trigger_event="debt_opened",
        fingerprint=lesson_fingerprint(
            "debt_opened",
            {"kind": "requirement_met", "target": "x", "reason": "no receipt"},
        ),
        action_text="不要声称 test_passed",
        source_task="task-1",
    )
    base.update(kwargs)
    return Lesson(**base)


# ── the fingerprint ───────────────────────────────────────────────


def test_the_fingerprint_is_deterministic() -> None:
    a = lesson_fingerprint("debt_opened", {"kind": "requirement_met", "target": "x"})
    b = lesson_fingerprint("debt_opened", {"target": "x", "kind": "requirement_met"})

    assert a == b  # field order never matters


def test_the_fingerprint_distinguishes_failures() -> None:
    a = lesson_fingerprint("debt_opened", {"kind": "requirement_met"})
    b = lesson_fingerprint("debt_opened", {"kind": "test_passed"})

    assert a != b


def test_the_shape_names_the_structural_subject() -> None:
    """A family is event + a code-owned subject, never prose."""

    from xenon.kernel.lessons import lesson_shape

    assert (
        lesson_shape(
            "policy_decision",
            {"kind": "veto", "capability": "shell", "reason": "x"},
        ).key
        == "policy_decision|shell"
    )
    # The gate writes ``scope``; the subject falls back to its first entry so
    # two denials of one capability become one family.
    assert (
        lesson_shape(
            "policy_decision",
            {"kind": "veto", "scope": ["vcs", "git commit"], "reason": "x"},
        ).key
        == "policy_decision|vcs"
    )
    assert (
        lesson_shape(
            "debt_opened",
            {"kind": "requirement_met", "target": "t", "reason": "r"},
        ).key
        == "debt_opened|requirement_met"
    )
    assert (
        lesson_shape(
            "loop_detected", {"signal": "repeat_call", "reason": "r"}
        ).key
        == "loop_detected|repeat_call"
    )
    assert (
        lesson_shape(
            "user_approval",
            {"approved": False, "tool": "shell", "reason": "r"},
        ).key
        == "user_approval|shell"
    )


def test_a_proposal_carries_its_shape() -> None:
    proposal = propose_lesson(
        "user_approval", {"approved": False, "tool": "shell", "reason": "no"}
    )

    assert proposal is not None
    assert proposal.shape == "user_approval|shell"


# ── the lesson ↔ memory projection ───────────────────────────────


def test_a_lesson_projects_onto_a_memory_record() -> None:
    from xenon.memory.models import MemoryKind, MemoryStatus

    record = record_from_lesson(_lesson(recurrences=2))

    assert record.kind is MemoryKind.LESSON
    assert record.status is MemoryStatus.ACTIVE
    assert record.content == "不要声称 test_passed"
    assert f"fp:{_lesson().fingerprint}" in record.tags
    assert "lesson:pitfall" in record.tags
    assert "recurrences:2" in record.tags
    assert record.evidence == "task-1"


def test_a_retired_lesson_projects_as_archived() -> None:
    from xenon.memory.models import MemoryStatus

    record = record_from_lesson(_lesson(retired=True))

    assert record.status is MemoryStatus.ARCHIVED


def test_lessons_round_trip_through_memory_records() -> None:
    lesson = _lesson(applied=2, recurrences=1)
    rebuilt = lesson_from_record(record_from_lesson(lesson))

    assert rebuilt.id == lesson.id
    assert rebuilt.action_text == lesson.action_text
    assert rebuilt.applied == 2
    assert rebuilt.recurrences == 1
    assert rebuilt.fingerprint == lesson.fingerprint


# ── the store: reading ───────────────────────────────────────────


def test_an_empty_store_matches_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert len(store) == 0
    assert store.match("debt_opened", {"kind": "requirement_met"}) is None


def test_a_matching_fingerprint_is_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_lesson())

    found = store.match(
        "debt_opened",
        {"kind": "requirement_met", "target": "x", "reason": "no receipt"},
    )

    assert found is not None
    # add() keeps the lesson's own id; only store(proposal) derives one.
    assert found.id == "L-0001"


def test_a_different_failure_does_not_match(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_lesson())

    assert store.match("debt_opened", {"kind": "test_passed"}) is None


def test_lessons_persist_across_fresh_stores_on_the_same_service(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    LessonStore(service).add(_lesson(applied=2, recurrences=1))

    reloaded = LessonStore(service)

    assert len(reloaded) == 1
    lesson = next(iter(reloaded))
    assert lesson.applied == 2
    assert lesson.recurrences == 1


def test_retired_lessons_do_not_match(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_lesson(retired=True))

    assert store.match("debt_opened", {"kind": "requirement_met"}) is None


def test_stale_lessons_do_not_match(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_lesson(last_applied_at=1.0))

    # Same fingerprint, applied 31 days ago: stale -> no match.
    assert (
        store.match(
            "debt_opened",
            {"kind": "requirement_met", "target": "x"},
            clock=lambda: 1.0 + 31 * 86400,
        )
        is None
    )


# ── the store: writing ───────────────────────────────────────────


def test_store_is_idempotent_by_fingerprint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    proposal = propose_lesson(
        "debt_opened",
        {"kind": "requirement_met", "target": "x", "reason": "no receipt"},
        source_task="task-1",
    )
    assert proposal is not None

    first = store.store(proposal)
    second = store.store(proposal)

    assert first is not None and second is not None
    # Second call rebuilds the lesson from memory; it is the same lesson by
    # fingerprint/id, not the same Python object.
    assert second.id == first.id
    assert len(store) == 1


def test_store_rejects_proposals_without_fingerprint_or_text(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    class P:
        fingerprint = ""
        action_text = "x"

    class Q:
        fingerprint = "fp"
        action_text = ""

    assert store.store(P()) is None
    assert store.store(Q()) is None


def test_recurrences_retire_a_lesson(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lesson = _lesson()
    store.add(lesson)

    assert store.record_recurrence(lesson.id, retire_after=3) is False
    assert store.record_recurrence(lesson.id, retire_after=3) is False
    assert store.record_recurrence(lesson.id, retire_after=3) is True

    # Counters live on the stored record; re-read, the local object is stale.
    stored = store._find(lesson.id)
    assert stored is not None
    assert stored.recurrences == 3
    assert stored.retired is True
    assert store.match("debt_opened", {"kind": "requirement_met"}) is None


def test_application_counting(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lesson = _lesson()
    store.add(lesson)

    assert store.record_application(lesson.id, clock=lambda: 42.0) is True

    stored = store._find(lesson.id)
    assert stored is not None
    assert stored.applied == 1
    assert stored.last_applied_at == 42.0


def test_a_retired_lesson_is_not_recounted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_lesson(retired=True))

    assert store.record_recurrence("L-0001") is False


def test_operations_on_an_unknown_lesson_are_noops(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.record_application("nope") is False
    assert store.record_recurrence("nope") is False
    assert store.retire("nope") is False
    assert store.active() == ()


def test_rows_expose_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_lesson())

    row = store.rows()[0]

    assert row["id"] == "L-0001"
    assert row["kind"] == "pitfall"
    assert row["trigger"] == "debt_opened"
    assert row["retired"] is False
    assert row["text"] == "不要声称 test_passed"


# ── match_for_task ───────────────────────────────────────────────


def test_match_for_task_is_bounded_and_recent_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = time.time()
    for index in range(5):
        store.add(
            _lesson(
                id=f"L-{index:04d}",
                fingerprint=f"fp-{index}",
                last_applied_at=now - 1000 + index,
            )
        )

    selected = store.match_for_task(limit=3)

    assert len(selected) == 3
    # Most recently applied first; add() keeps the supplied ids.
    assert [lesson.id for lesson in selected] == ["L-0004", "L-0003", "L-0002"]


def test_parameter_lessons_ride_as_advice(tmp_path: Path) -> None:
    """A parameter lesson is carried as text; nothing mutates a budget.

    Excluding it meant a loop-detection lesson was stored and then never
    shown, which is the "it learned but nothing changed" failure.
    """

    store = _store(tmp_path)
    store.add(_lesson(id="L-1", kind="pitfall", fingerprint="fp-1"))
    store.add(_lesson(id="L-2", kind="parameter", fingerprint="fp-2"))
    store.add(_lesson(id="L-3", kind="preference", fingerprint="fp-3"))

    selected = store.match_for_task()

    assert {lesson.kind for lesson in selected} == {
        "pitfall",
        "parameter",
        "preference",
    }


def test_a_capability_lesson_is_skipped_when_the_catalog_cannot_bind_it(
    tmp_path: Path,
) -> None:
    """Relevance is structural: the subject either exists in the catalog or not."""

    store = _store(tmp_path)
    store.add(
        _lesson(
            id="L-shell",
            fingerprint="fp-shell",
            trigger_event="policy_decision",
            shape="policy_decision|shell",
        )
    )
    store.add(
        _lesson(
            id="L-debt",
            fingerprint="fp-debt",
            shape="debt_opened|requirement_met",
        )
    )

    selected = store.match_for_task(available={"read", "edit"})
    assert [lesson.id for lesson in selected] == ["L-debt"]

    selected = store.match_for_task(available={"read", "shell"})
    assert {lesson.id for lesson in selected} == {"L-shell", "L-debt"}


def test_an_empty_catalog_fails_open(tmp_path: Path) -> None:
    """No catalog information must never silently drop a safety lesson."""

    store = _store(tmp_path)
    store.add(
        _lesson(
            id="L-shell",
            fingerprint="fp-shell",
            shape="policy_decision|shell",
        )
    )

    assert [lesson.id for lesson in store.match_for_task(available=())] == [
        "L-shell"
    ]


# ── summarize: repeated failures become one rule ─────────────────


def _shaped_lesson(index: int, shape: str = "policy_decision|shell") -> Lesson:
    return _lesson(
        id=f"L-{index:04d}",
        fingerprint=f"fp-{index}",
        trigger_event=shape.split("|", 1)[0],
        shape=shape,
    )


def test_repeated_failures_of_one_shape_summarize_into_one_rule(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    for index in range(2):
        store.add(_shaped_lesson(index))

    created = store.summarize()

    assert [rule.kind for rule in created] == ["rule"]
    rule = created[0]
    assert rule.shape == "policy_decision|shell"
    assert rule.state == "hypothesis"
    assert rule.derived_from == ("L-0000", "L-0001")
    assert "shell" in rule.action_text

    # The summary replaces the enumeration: one line, not two.
    selected = store.match_for_task(available={"shell"})
    assert [lesson.id for lesson in selected] == [rule.id]

    # Re-summarizing the same family changes nothing.
    assert store.summarize() == []


def test_a_retired_rule_is_not_resurrected_by_summarize(tmp_path: Path) -> None:
    """A rule that failed is evidence for a design change, not for a reword."""

    store = _store(tmp_path)
    for index in range(2):
        store.add(_shaped_lesson(index))
    rule = store.summarize()[0]
    store.retire(rule.id)

    assert store.summarize() == []
    assert store.get(rule.id) is not None
    assert store.get(rule.id).retired is True


def test_a_rule_earns_state_by_surviving_applications(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(2):
        store.add(_shaped_lesson(index))
    rule = store.summarize()[0]

    # Applied once is not enough evidence to promote a hypothesis.
    assert store.record_confirmation(rule.id) is None
    store.record_application(rule.id)
    assert store.record_confirmation(rule.id) is None
    store.record_application(rule.id)
    assert store.record_confirmation(rule.id) == "active"
    for _ in range(3):
        store.record_application(rule.id)
    assert store.record_confirmation(rule.id) == "proven"
    assert store.get(rule.id).state == "proven"


def test_a_rule_recurs_when_any_member_shape_fails_again(
    tmp_path: Path,
) -> None:
    from xenon.harness.lessons import (
        design_debt_payload,
        record_recurrences,
    )
    from xenon.ledger.store import Ledger

    store = _store(tmp_path)
    for index in range(2):
        store.add(_shaped_lesson(index))
    rule = store.summarize()[0]
    ledger = Ledger(tmp_path / "t.jsonl", "t1")
    ledger.append(
        EventKind.LESSON_APPLIED, EventSource.GATE, {"lesson_id": rule.id}
    )
    ledger.append(
        EventKind.POLICY_DECISION,
        EventSource.GATE,
        {"kind": "veto", "capability": "shell", "reason": "denied"},
    )

    assert record_recurrences(ledger, store, retire_after=1) == [rule.id]

    debt = design_debt_payload(store, rule.id)
    assert debt is not None
    assert debt["subject"] == "shell"
    assert debt["reason"] == "lesson_retired_after_recurrence"


def test_an_approved_tool_is_not_a_failure_family(tmp_path: Path) -> None:
    """Only harness-judged *failures* count; a normal approval is not one."""

    from xenon.harness.lessons import failure_shapes
    from xenon.ledger.store import Ledger

    ledger = Ledger(tmp_path / "t.jsonl", "t1")
    ledger.append(
        EventKind.USER_APPROVAL,
        EventSource.USER,
        {"approved": True, "tool": "shell", "reason": "ok"},
    )
    ledger.append(
        EventKind.POLICY_DECISION,
        EventSource.GATE,
        {"kind": "allow", "capability": "shell", "reason": "session grant"},
    )

    assert failure_shapes(ledger) == {}


def test_summarize_and_confirm_reads_the_ledger(tmp_path: Path) -> None:
    from xenon.harness.lessons import (
        summarize_and_confirm,
    )
    from xenon.ledger.store import Ledger

    store = _store(tmp_path)
    for index in range(2):
        store.add(_shaped_lesson(index))

    # First task: the family is summarized; nothing was applied, so no rule
    # can be confirmed yet.
    first = Ledger(tmp_path / "t1.jsonl", "t1")
    created, advanced = summarize_and_confirm(first, store)
    assert len(created) == 1
    assert advanced == []

    # Second task applied the rule and hit no same-shape failure.
    rule_id = created[0].id
    store.record_application(rule_id)
    store.record_application(rule_id)
    second = Ledger(tmp_path / "t2.jsonl", "t2")
    second.append(
        EventKind.LESSON_APPLIED, EventSource.GATE, {"lesson_id": rule_id}
    )
    created, advanced = summarize_and_confirm(second, store)
    assert created == []
    assert advanced == [(rule_id, "active")]


def test_a_failure_before_the_application_is_not_a_recurrence(
    tmp_path: Path,
) -> None:
    """Recurrence means "failed *after* the lesson was carried".

    The ledger holds the failure that taught the lesson (or the plan phase of
    the same user task).  Counting by membership alone treated those as fresh
    recurrences and could retire a lesson the moment it was stored.
    """

    from xenon.harness.lessons import record_recurrences
    from xenon.ledger.store import Ledger

    store = _store(tmp_path)
    failure = {
        "kind": "veto",
        "capability": "shell",
        "reason": "denied",
    }
    store.add(
        _lesson(
            id="L-shell",
            shape="policy_decision|shell",
            fingerprint=lesson_fingerprint("policy_decision", failure),
        )
    )
    ledger = Ledger(tmp_path / "t.jsonl", "t1")
    ledger.append(EventKind.POLICY_DECISION, EventSource.GATE, failure)
    ledger.append(
        EventKind.LESSON_APPLIED, EventSource.GATE, {"lesson_id": "L-shell"}
    )
    assert record_recurrences(ledger, store, retire_after=1) == []

    ledger.append(EventKind.POLICY_DECISION, EventSource.GATE, failure)
    assert record_recurrences(ledger, store, retire_after=2) == []
    assert store.get("L-shell").recurrences == 1


def test_a_second_application_phase_does_not_recount_the_plan_failures(
    tmp_path: Path,
) -> None:
    """Plan + approved resume is one task: failures are counted once."""

    from xenon.harness.lessons import record_recurrences
    from xenon.ledger.store import Ledger

    store = _store(tmp_path)
    failure = {
        "kind": "veto",
        "capability": "shell",
        "reason": "denied",
    }
    store.add(
        _lesson(
            id="L-shell",
            shape="policy_decision|shell",
            fingerprint=lesson_fingerprint("policy_decision", failure),
        )
    )
    ledger = Ledger(tmp_path / "t.jsonl", "t1")
    ledger.append(
        EventKind.LESSON_APPLIED, EventSource.GATE, {"lesson_id": "L-shell"}
    )
    ledger.append(EventKind.POLICY_DECISION, EventSource.GATE, failure)
    ledger.append(
        EventKind.LESSON_APPLIED, EventSource.GATE, {"lesson_id": "L-shell"}
    )
    # Only this failure happened inside the resumed execution.
    ledger.append(EventKind.POLICY_DECISION, EventSource.GATE, failure)

    assert record_recurrences(ledger, store, retire_after=2) == []
    assert store.get("L-shell").recurrences == 1


def test_a_real_loop_event_is_the_same_failure_as_its_proposal() -> None:
    """The runtime writes ``kind``; the proposer was called with ``signal``.

    Reading only ``signal`` rebuilt a different fingerprint from the very
    event that proposed the lesson, so loop lessons could never recur.
    """

    from xenon.kernel.lessons import lesson_shape

    event_payload = {
        "kind": "identical_call",
        "detail": "same call x5",
        "tool": "read",
        "reason": "same call x5",
        "correction_attempt": 1,
    }
    proposal = propose_lesson(
        "loop_detected",
        {"signal": "identical_call", "reason": "same call x5"},
    )
    assert proposal is not None
    from xenon.kernel.lessons import canonical_failure_payload

    assert (
        lesson_fingerprint(
            "loop_detected",
            canonical_failure_payload("loop_detected", event_payload),
        )
        == proposal.fingerprint
    )
    assert lesson_shape("loop_detected", event_payload).key == proposal.shape


def test_stale_lessons_do_not_ride_into_a_task(tmp_path: Path) -> None:
    """Guardrail 2's 30-day cutoff applies on the production path too."""

    store = _store(tmp_path)
    now = time.time()
    store.add(
        _lesson(
            id="L-old",
            fingerprint="fp-old",
            last_applied_at=now - (31 * 86400),
        )
    )
    store.add(
        _lesson(
            id="L-new",
            fingerprint="fp-new",
            last_applied_at=now - 60,
        )
    )
    store.add(_lesson(id="L-never", fingerprint="fp-never"))

    assert {lesson.id for lesson in store.match_for_task()} == {
        "L-new",
        "L-never",
    }


def test_lessons_from_other_scopes_do_not_leak_into_a_project(
    tmp_path: Path,
) -> None:
    """Writes are PROJECT_LOCAL; reads must be too."""

    from xenon.memory import MemoryBackendRegistry, MemoryService
    from xenon.memory.models import MemoryKind, MemoryScope

    # Isolated user root: a test must never write into the operator's real
    # global memory (this one did, once).
    service = MemoryService(
        MemoryBackendRegistry(tmp_path, user_data_root=tmp_path / "user-memory")
    )
    store = LessonStore(service)
    service.remember(
        "global lesson",
        scope=MemoryScope.USER,
        kind=MemoryKind.LESSON,
        tags=["lesson:pitfall", "fp:global"],
    )

    assert store.match_for_task() == ()


def test_an_archived_lesson_is_not_applied_or_resurrected(
    tmp_path: Path,
) -> None:
    from xenon.memory.models import MemoryScope, MemoryStatus

    store = _store(tmp_path)
    store.add(_lesson(id="L-1", fingerprint="fp-1"))
    assert store.record_application("L-1") is True
    assert store.service.archive("L-1") is True

    assert store.match_for_task() == ()
    assert store.record_application("L-1") is False
    record = next(
        item
        for item in store.service.list_records(
            scope=MemoryScope.PROJECT_LOCAL, include_archived=True
        )
        if item.id == "L-1"
    )
    assert record.status is MemoryStatus.ARCHIVED


def test_application_preserves_user_metadata(tmp_path: Path) -> None:
    from xenon.memory.models import MemoryScope

    store = _store(tmp_path)
    store.add(_lesson(id="L-1", fingerprint="fp-1"))
    assert store.service.set_pinned("L-1", True) is True
    lesson = store.get("L-1")
    lesson.counterfactual = "回执机制改变后失效"
    store.add(lesson)

    store.record_application("L-1")

    record = next(
        item
        for item in store.service.list_records(
            scope=MemoryScope.PROJECT_LOCAL, include_archived=True
        )
        if item.id == "L-1"
    )
    assert record.pinned is True
    assert store.get("L-1").counterfactual == "回执机制改变后失效"


def test_a_retired_rule_does_not_re_expose_its_members(
    tmp_path: Path,
) -> None:
    """The failed summary's guidance must not come back as an enumeration."""

    store = _store(tmp_path)
    for index in range(2):
        store.add(_shaped_lesson(index))
    rule = store.summarize()[0]
    assert store.retire(rule.id) is True

    assert store.match_for_task() == ()


def test_match_for_task_excludes_retired_lessons(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_lesson(id="L-1", fingerprint="fp-1", retired=True))
    store.add(_lesson(id="L-2", fingerprint="fp-2"))

    assert [lesson.id for lesson in store.match_for_task()] == ["L-2"]


def test_match_for_task_zero_limit_disables(tmp_path: Path) -> None:
    """#369 review: limit=0 means no lessons, not a minimum of one."""
    store = _store(tmp_path)
    store.add(_lesson(id="L-1", kind="pitfall", fingerprint="fp-1"))

    assert store.match_for_task(limit=0) == ()
    assert store.match_for_task(limit=-1) == ()


# ── store_proposals: ledger proposals become memory ─────────────


def _proposal_payload(event_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    proposal = propose_lesson(event_kind, payload, source_task="task-1")
    assert proposal is not None
    return {
        "kind": proposal.kind,
        "trigger_event": proposal.trigger_event,
        "fingerprint": proposal.fingerprint,
        "action_text": proposal.action_text,
        "counterfactual": proposal.counterfactual,
        "source_task": proposal.source_task,
        "shape": getattr(proposal, "shape", ""),
    }


def test_store_proposals_persists_lessons_and_reports_new_ids(
    tmp_path: Path,
) -> None:
    from xenon.harness.lessons import store_proposals
    from xenon.ledger.store import Ledger

    ledger = Ledger.in_memory("t1")
    ledger.append(
        EventKind.LESSON_PROPOSED,
        EventSource.GATE,
        _proposal_payload(
            "debt_opened",
            {"kind": "requirement_met", "target": "x", "reason": "no receipt"},
        ),
    )
    store = _store(tmp_path)

    created = store_proposals(ledger, store)

    assert len(created) == 1
    assert len(store) == 1
    from xenon.memory.models import MemoryKind

    records = store.service.list_records(include_archived=True)
    assert [record.kind for record in records] == [MemoryKind.LESSON]


def test_store_proposals_is_idempotent(tmp_path: Path) -> None:
    from xenon.harness.lessons import store_proposals
    from xenon.ledger.store import Ledger

    payload = _proposal_payload(
        "debt_opened",
        {"kind": "requirement_met", "target": "x", "reason": "no receipt"},
    )
    ledger = Ledger.in_memory("t1")
    ledger.append(EventKind.LESSON_PROPOSED, EventSource.GATE, payload)
    store = _store(tmp_path)

    assert len(store_proposals(ledger, store)) == 1
    # Same proposal again: lesson already carries that fingerprint -> not new.
    assert store_proposals(ledger, store) == []


def test_store_proposals_ignores_incomplete_proposals(tmp_path: Path) -> None:
    from xenon.harness.lessons import store_proposals
    from xenon.ledger.store import Ledger

    ledger = Ledger.in_memory("t1")
    ledger.append(
        EventKind.LESSON_PROPOSED,
        EventSource.GATE,
        {"kind": "pitfall", "fingerprint": "fp", "action_text": ""},
    )

    assert store_proposals(ledger, _store(tmp_path)) == []


def test_store_proposals_ignores_non_lesson_stores() -> None:
    from xenon.harness.lessons import store_proposals
    from xenon.ledger.store import Ledger

    assert store_proposals(Ledger.in_memory("t1"), "not-a-store") == []


def test_store_proposals_swallows_an_unreadable_ledger(tmp_path: Path) -> None:
    from xenon.harness.lessons import store_proposals

    class BadLedger:
        def read(self):
            raise OSError("gone")

    assert store_proposals(BadLedger(), _store(tmp_path)) == []


# ── record_recurrences ──────────────────────────────────────────


def test_record_recurrences_counts_and_retires(tmp_path: Path) -> None:
    from xenon.harness.lessons import record_recurrences
    from xenon.ledger.store import Ledger

    store = _store(tmp_path)
    lesson = _lesson()
    store.add(lesson)
    ledger = Ledger.in_memory("t1")
    # The lesson was applied, then the same failure recurred twice more.
    ledger.append(
        EventKind.LESSON_APPLIED,
        EventSource.GATE,
        {"lesson_id": lesson.id},
    )
    for _ in range(2):
        ledger.append(
            EventKind.DEBT_OPENED,
            EventSource.GATE,
            {"kind": "requirement_met", "target": "x", "reason": "no receipt"},
        )

    assert record_recurrences(ledger, store) == []  # default threshold is 3
    stored = store._find(lesson.id)
    assert stored is not None and stored.recurrences == 2
    assert store.retire(lesson.id) is True
    assert store._find(lesson.id) is not None
    assert store._find(lesson.id).retired is True


def test_record_recurrences_ignores_non_lesson_stores() -> None:
    from xenon.harness.lessons import record_recurrences
    from xenon.ledger.store import Ledger

    assert record_recurrences(Ledger.in_memory("t1"), "not-a-store") == []


def test_record_recurrences_needs_an_application_first(tmp_path: Path) -> None:
    from xenon.harness.lessons import record_recurrences
    from xenon.ledger.store import Ledger

    store = _store(tmp_path)
    store.add(_lesson())
    ledger = Ledger.in_memory("t2")
    ledger.append(
        EventKind.DEBT_OPENED,
        EventSource.GATE,
        {"kind": "requirement_met", "target": "x", "reason": "no receipt"},
    )

    # No LESSON_APPLIED -> the failure is not a recurrence of an applied lesson.
    assert record_recurrences(ledger, store) == []


def test_record_recurrences_skips_non_veto_policy_and_approved(
    tmp_path: Path,
) -> None:
    from xenon.harness.lessons import record_recurrences
    from xenon.ledger.store import Ledger

    store = _store(tmp_path)
    ledger = Ledger.in_memory("t2")
    ledger.append(
        EventKind.POLICY_DECISION, EventSource.GATE, {"kind": "allow"}
    )
    ledger.append(
        EventKind.USER_APPROVAL, EventSource.USER, {"approved": True}
    )

    assert record_recurrences(ledger, store) == []


def test_a_broken_proposer_in_recurrence_scan_is_skipped(
    tmp_path: Path,
) -> None:
    from xenon.harness.lessons import record_recurrences
    from xenon.ledger.store import Ledger

    store = _store(tmp_path)
    ledger = Ledger.in_memory("t3")
    ledger.append(
        EventKind.DEBT_OPENED,
        EventSource.GATE,
        {"kind": "requirement_met", "target": "x", "reason": "no receipt"},
    )
    import xenon.kernel.lesson_proposer as proposer_module

    original = proposer_module.propose_lesson
    proposer_module.propose_lesson = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("proposer broke")
    )
    try:
        assert record_recurrences(ledger, store) == []
    finally:
        proposer_module.propose_lesson = original


# ── the adapter activation ──────────────────────────────────────


def test_lesson_store_for_returns_none_without_a_memory_service() -> None:
    from xenon.repl.runtime_adapter import _lesson_store_for

    class Fake: ...

    assert _lesson_store_for(Fake()) is None


def test_lesson_store_for_builds_and_caches_one_store(
    tmp_path: Path,
) -> None:
    from xenon.repl.runtime_adapter import _lesson_store_for

    class MiniRepl:
        _lesson_store: Any = None

        def _get_memory_service(self) -> Any:
            return _service(tmp_path)

    repl = MiniRepl()
    store = _lesson_store_for(repl)

    assert isinstance(store, LessonStore)
    assert _lesson_store_for(repl) is store  # shared across turns


def test_lesson_store_for_swallows_a_broken_memory_service() -> None:
    from xenon.repl.runtime_adapter import _lesson_store_for

    class Broken:
        def _get_memory_service(self) -> Any:
            raise RuntimeError("no memory")

    assert _lesson_store_for(Broken()) is None


# ── runner integration ───────────────────────────────────────────


def _fake_client() -> Any:
    class FakeClient:
        def __call__(self, model_id, messages, **kwargs):
            return json.dumps({"action": "finish", "answer": "done"})

    return FakeClient()


def _llm_strategy(client: Any) -> Any:
    from xenon.harness.llm import ModelConfig, build_llm_loop_strategy
    from xenon.kernel.strategies import LoopProfile

    return build_llm_loop_strategy(
        ModelConfig(model_id="deepseek/deepseek-v4-flash"),
        LoopProfile(),
        client=client,
    )


def test_run_task_injects_bounded_lessons_as_constraints(
    tmp_path: Path,
) -> None:
    from tests.harness.test_exec import _repo
    from xenon.harness.runner import run_task
    from xenon.ledger.store import Ledger

    _repo(tmp_path)
    store = _store(tmp_path)
    now = time.time()
    for index in range(4):
        store.add(
            _lesson(
                id=f"L-{index}",
                fingerprint=f"fp-{index}",
                last_applied_at=now - 1000 + index,
            )
        )

    run = run_task(
        "read README and report",
        strategy=_llm_strategy(_fake_client()),
        workspace=tmp_path,
        ledger_dir=tmp_path / "ledger",
        task_id="lesson-applied",
        max_steps=3,
        lesson_store=store,
    )

    events = Ledger(run.ledger_path, "lesson-applied").read()
    applied = [e for e in events if e.kind is EventKind.LESSON_APPLIED]
    assert len(applied) == 3, "only the bounded subset is applied"
    assert [e.payload["lesson_id"] for e in applied] == ["L-3", "L-2", "L-1"]


def test_retired_lessons_are_not_applied(tmp_path: Path) -> None:
    from tests.harness.test_exec import _repo
    from xenon.harness.runner import run_task
    from xenon.ledger.store import Ledger

    _repo(tmp_path)
    store = _store(tmp_path)
    store.add(_lesson(id="L-1", fingerprint="fp-1"))
    store.add(_lesson(id="L-2", fingerprint="fp-2", retired=True))

    run = run_task(
        "read README and report",
        strategy=_llm_strategy(_fake_client()),
        workspace=tmp_path,
        ledger_dir=tmp_path / "ledger",
        task_id="lesson-no-retired",
        max_steps=3,
        lesson_store=store,
    )

    applied = [
        e.payload["lesson_id"]
        for e in Ledger(run.ledger_path, "lesson-no-retired").read()
        if e.kind is EventKind.LESSON_APPLIED
    ]
    assert "L-2" not in applied


def test_run_task_stores_proposed_lessons_end_to_end(tmp_path: Path) -> None:
    """The chain that had 621 ledgers with proposals and 0 stored lessons.

    A task opens a debt -> the kernel writes LESSON_PROPOSED -> the runner
    stores it and writes LESSON_STORED -> the memory service holds one LESSON.
    """

    from tests.harness.test_exec import _repo
    from xenon.harness.runner import run_task
    from xenon.ledger.store import Ledger
    from xenon.memory.models import MemoryKind

    _repo(tmp_path)
    store = _store(tmp_path)

    class DebtClient:
        """First call edits, second call finishes with an unverifiable claim."""

        def __init__(self) -> None:
            self.i = 0

        def __call__(self, model_id, messages, **kwargs):
            self.i += 1
            if self.i == 1:
                return json.dumps(
                    {
                        "action": "call_tool",
                        "capability": "edit",
                        "params": {
                            "files": [
                                {
                                    "path": "calc/add.py",
                                    "content": "def add(a, b):"
                                    + chr(10)
                                    + "    return a + b"
                                    + chr(10),
                                }
                            ]
                        },
                    }
                )
            return json.dumps(
                {
                    "action": "finish",
                    "answer": "done",
                    "claims": [
                        {
                            "id": "c1",
                            "kind": "tool_succeeded",
                            "target": "echo hi",
                        }
                    ],
                }
            )

    run = run_task(
        "edit calc/add.py then run a test",
        strategy=_llm_strategy(DebtClient()),
        workspace=tmp_path,
        ledger_dir=tmp_path / "ledger",
        task_id="lesson-store",
        max_steps=4,
        lesson_store=store,
    )

    events = Ledger(run.ledger_path, "lesson-store").read()
    kinds = [event.kind for event in events]
    assert EventKind.LESSON_PROPOSED in kinds
    assert EventKind.LESSON_STORED in kinds

    records = store.service.list_records(include_archived=True)
    lesson_records = [r for r in records if r.kind is MemoryKind.LESSON]
    assert len(lesson_records) == 1


def test_a_family_of_failures_summarizes_into_a_rule_end_to_end(
    tmp_path: Path,
) -> None:
    """Two same-shape failures -> one rule -> the next task carries the rule.

    This is the "总结" chain: the runtime proposes, the runner stores, then
    clusters by structural family, and the constraint section shows one
    summary instead of the enumeration.
    """

    from tests.harness.test_exec import _repo
    from xenon.harness.runner import run_task
    from xenon.ledger.store import Ledger

    _repo(tmp_path)
    store = _store(tmp_path)

    class ClaimClient:
        """Edits once, then claims a command that never ran."""

        def __init__(self, target: str) -> None:
            self.target = target
            self.i = 0

        def __call__(self, model_id, messages, **kwargs):
            self.i += 1
            if self.i == 1:
                return json.dumps(
                    {
                        "action": "call_tool",
                        "capability": "edit",
                        "params": {
                            "files": [
                                {
                                    "path": "calc/add.py",
                                    "content": "def add(a, b):"
                                    + chr(10)
                                    + "    return a + b"
                                    + chr(10),
                                }
                            ]
                        },
                    }
                )
            return json.dumps(
                {
                    "action": "finish",
                    "answer": "done",
                    "claims": [
                        {
                            "id": "c1",
                            "kind": "tool_succeeded",
                            "target": self.target,
                        }
                    ],
                }
            )

    for task_id, target in (
        ("family-a", "echo alpha"),
        ("family-b", "echo beta"),
    ):
        run_task(
            f"edit calc/add.py then claim {target}",
            strategy=_llm_strategy(ClaimClient(target)),
            workspace=tmp_path,
            ledger_dir=tmp_path / "ledger",
            task_id=task_id,
            max_steps=4,
            lesson_store=store,
        )

    rules = [lesson for lesson in store if lesson.is_rule]
    assert len(rules) == 1
    rule = rules[0]
    assert rule.shape == "debt_opened|requirement_met"
    assert len(rule.derived_from) == 2
    assert rule.state == "hypothesis"

    second = Ledger(tmp_path / "ledger" / "family-b.jsonl", "family-b").read()
    clustered = [event for event in second if event.kind is EventKind.LESSON_CLUSTERED]
    assert len(clustered) == 1, "the rule is written into the ledger that formed it"
    assert clustered[0].payload["shape"] == rule.shape

    # The next task carries the summary; the two primitives stay in the store
    # as evidence but are not enumerated into the prompt.
    third = run_task(
        "read README and report",
        strategy=_llm_strategy(_fake_client()),
        workspace=tmp_path,
        ledger_dir=tmp_path / "ledger",
        task_id="family-c",
        max_steps=3,
        lesson_store=store,
    )
    applied = [
        event.payload["lesson_id"]
        for event in Ledger(third.ledger_path, "family-c").read()
        if event.kind is EventKind.LESSON_APPLIED
    ]
    assert applied == [rule.id]


def test_recurrence_retires_and_runner_writes_the_event(
    tmp_path: Path,
) -> None:
    """Full loop: a lesson applied, the same failure recurs for the 3rd time,
    the lesson is retired and LESSON_RETIRED lands in the task's ledger."""

    from tests.harness.test_exec import _repo
    from xenon.harness.runner import run_task
    from xenon.ledger.store import Ledger

    _repo(tmp_path)

    class DebtClient:
        """First call edits, second call finishes with an unverifiable claim."""

        def __init__(self) -> None:
            self.i = 0

        def __call__(self, model_id, messages, **kwargs):
            self.i += 1
            if self.i == 1:
                return json.dumps(
                    {
                        "action": "call_tool",
                        "capability": "edit",
                        "params": {
                            "files": [
                                {
                                    "path": "calc/add.py",
                                    "content": "def add(a, b):"
                                    + chr(10)
                                    + "    return a + b"
                                    + chr(10),
                                }
                            ]
                        },
                    }
                )
            return json.dumps(
                {
                    "action": "finish",
                    "answer": "done",
                    "claims": [
                        {
                            "id": "c1",
                            "kind": "tool_succeeded",
                            "target": "echo hi",
                        }
                    ],
                }
            )

    store = _store(tmp_path)
    # Pre-seed a lesson at recurrence count 2 whose fingerprint matches the
    # canonical debt shape this task opens — one more recurrence retires it.
    proposal = propose_lesson(
        "debt_opened",
        {
            "kind": "requirement_met",
            "target": "",
            "reason": "receipt status is not indexed as a claim fact",
        },
    )
    assert proposal is not None
    lesson = Lesson(
        id=lesson_id(proposal.fingerprint),
        kind="pitfall",
        trigger_event="debt_opened",
        fingerprint=proposal.fingerprint,
        action_text=proposal.action_text,
        source_task="earlier-task",
        recurrences=2,
    )
    store.add(lesson)

    run = run_task(
        "edit calc/add.py then run a test",
        strategy=_llm_strategy(DebtClient()),
        workspace=tmp_path,
        ledger_dir=tmp_path / "ledger",
        task_id="lesson-retire",
        max_steps=4,
        lesson_store=store,
    )

    events = Ledger(run.ledger_path, "lesson-retire").read()
    kinds = [event.kind.value for event in events]
    assert "lesson_retired" in kinds, kinds
    stored = store._find(lesson.id)
    assert stored is not None and stored.retired is True


# ── runner failure-path guards ──────────────────────────────────


def test_a_broken_lesson_store_does_not_fail_the_task(tmp_path: Path) -> None:
    from tests.harness.test_exec import _repo
    from xenon.harness.runner import run_task

    _repo(tmp_path)
    store = _store(tmp_path)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("store broke")

    store.match_for_task = boom
    store.record_application = boom

    run = run_task(
        "read README and report",
        strategy=_llm_strategy(_fake_client()),
        workspace=tmp_path,
        ledger_dir=tmp_path / "ledger",
        task_id="lesson-broken-store",
        max_steps=3,
        lesson_store=store,
    )

    assert run.result is not None


def test_a_broken_recurrence_counter_does_not_fail_the_task(
    tmp_path: Path,
) -> None:
    import xenon.harness.learning as learning_module
    from tests.harness.test_exec import _repo
    from xenon.harness.runner import run_task

    _repo(tmp_path)
    store = _store(tmp_path)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("counter broke")

    original = learning_module.record_recurrences
    learning_module.record_recurrences = boom
    try:
        run = run_task(
            "read README and report",
            strategy=_llm_strategy(_fake_client()),
            workspace=tmp_path,
            ledger_dir=tmp_path / "ledger",
            task_id="lesson-broken-counter",
            max_steps=3,
            lesson_store=store,
        )
    finally:
        learning_module.record_recurrences = original

    assert run.result is not None


def test_a_broken_application_counter_does_not_fail_the_task(
    tmp_path: Path,
) -> None:
    from tests.harness.test_exec import _repo
    from xenon.harness.runner import run_task
    from xenon.ledger.store import Ledger

    _repo(tmp_path)
    store = _store(tmp_path)
    store.add(_lesson(id="L-1", fingerprint="fp-1"))

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("counter broke")

    store.record_application = boom

    run = run_task(
        "read README and report",
        strategy=_llm_strategy(_fake_client()),
        workspace=tmp_path,
        ledger_dir=tmp_path / "ledger",
        task_id="lesson-broken-app-counter",
        max_steps=3,
        lesson_store=store,
    )

    assert run.result is not None
    applied = [
        e
        for e in Ledger(run.ledger_path, "lesson-broken-app-counter").read()
        if e.kind is EventKind.LESSON_APPLIED
    ]
    assert len(applied) == 1  # constraint still injected even if the count broke


# ── helpers ──────────────────────────────────────────────────────


def lesson_id(fingerprint: str) -> str:
    """The id the store derives for a fingerprint (mirrors ``_lesson_id``)."""

    return f"lsn-{fingerprint[:16]}"
