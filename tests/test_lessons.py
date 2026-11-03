"""Lesson store: slice 1 of self-evolution (docs/design/self-evolution.md).

Covers the storage + fingerprint + retirement semantics.  The proposer (slice
2), the applier (slice 3) and the PITFALLS.md projection (slice 4) wire the
same store; this file pins the rules they all rely on.
"""

from __future__ import annotations

import json
from pathlib import Path

from xenon.harness.lessons import LessonStore
from xenon.kernel.lessons import (
    Lesson,
    lesson_fingerprint,
)


def _lesson(**kwargs) -> Lesson:
    base = dict(
        id="L-0001",
        kind="pitfall",
        trigger_event="debt_opened",
        fingerprint=lesson_fingerprint(
            "debt_opened", {"kind": "requirement_met", "target": "x"}
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


# ── the store ─────────────────────────────────────────────────────


def test_an_empty_store_matches_nothing(tmp_path: Path) -> None:
    store = LessonStore(tmp_path / "lessons.yaml")

    assert len(store) == 0
    assert store.match("debt_opened", {"kind": "requirement_met"}) is None


def test_a_matching_fingerprint_is_found(tmp_path: Path) -> None:
    store = LessonStore(tmp_path / "lessons.yaml")
    lesson = _lesson()
    store.add(lesson)

    found = store.match("debt_opened", {"kind": "requirement_met", "target": "x"})

    assert found is not None
    assert found.id == "L-0001"


def test_a_different_failure_does_not_match(tmp_path: Path) -> None:
    store = LessonStore(tmp_path / "lessons.yaml")
    store.add(_lesson())

    assert store.match("debt_opened", {"kind": "test_passed"}) is None


def test_lessons_round_trip_through_the_file(tmp_path: Path) -> None:
    path = tmp_path / "lessons.yaml"
    store = LessonStore(path)
    store.add(_lesson(applied=2, recurrences=1))
    store.save()

    reloaded = LessonStore(path)

    assert len(reloaded) == 1
    lesson = next(iter(reloaded))
    assert lesson.applied == 2
    assert lesson.recurrences == 1


def test_a_malformed_lesson_is_dropped_not_guessed(tmp_path: Path) -> None:
    path = tmp_path / "lessons.yaml"
    path.write_text(
        json.dumps(
            {"lessons": [{"id": "broken", "kind": "pitfall"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = LessonStore(path)

    assert len(store) == 0


def test_an_unreadable_file_is_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "lessons.yaml"
    path.write_text("not json", encoding="utf-8")

    store = LessonStore(path)

    assert len(store) == 0
    assert store.load_errors


def test_retired_lessons_do_not_match(tmp_path: Path) -> None:
    store = LessonStore(tmp_path / "lessons.yaml")
    store.add(_lesson(retired=True))

    assert store.match("debt_opened", {"kind": "requirement_met"}) is None


def test_stale_lessons_do_not_match(tmp_path: Path) -> None:
    store = LessonStore(tmp_path / "lessons.yaml")
    store.add(_lesson(last_applied_at=1.0))

    assert (
        store.match(
            "debt_opened",
            {"kind": "requirement_met"},
            clock=lambda: 1.0 + 31 * 86400,
        )
        is None
    )


def test_recurrences_retire_a_lesson(tmp_path: Path) -> None:
    store = LessonStore(tmp_path / "lessons.yaml")
    lesson = _lesson()
    store.add(lesson)

    retired = store.record_recurrence(lesson.id, retire_after=3)
    assert retired is False
    retired = store.record_recurrence(lesson.id, retire_after=3)
    assert retired is False
    retired = store.record_recurrence(lesson.id, retire_after=3)

    assert retired is True
    assert lesson.retired is True
    assert store.match("debt_opened", {"kind": "requirement_met"}) is None


def test_application_counting(tmp_path: Path) -> None:
    store = LessonStore(tmp_path / "lessons.yaml")
    lesson = _lesson()
    store.add(lesson)

    store.record_application(lesson.id, clock=lambda: 42.0)

    assert lesson.applied == 1
    assert lesson.last_applied_at == 42.0
