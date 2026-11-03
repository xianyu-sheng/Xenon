"""Lesson store: the durable, visible half of self-evolution (slice 1).

``docs/design/self-evolution.md``: lessons are stored in
``~/.xenon/lessons.yaml``, matched by a deterministic fingerprint, and retired
when recurrences prove them wrong.  This module lives outside the kernel
(harness layer) because it touches the filesystem — the kernel stays a pure
function of its inputs and only emits ``LESSON_*`` events.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Mapping

from xenon.kernel.lessons import (
    DEFAULT_RETIRE_AFTER,
    DEFAULT_STALE_DAYS,
    Lesson,
    lesson_fingerprint,
)

__all__ = ["LessonStore"]

_LESSONS_FILE = ".xenon/lessons.yaml"


class LessonStore:
    """``~/.xenon/lessons.yaml`` with load / save / match / retire."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = (
            Path(path) if path is not None else Path.home() / _LESSONS_FILE
        )
        self._lessons: list[Lesson] = []
        self.load_errors: list[str] = []
        self.load()

    def load(self) -> None:
        self._lessons = []
        self.load_errors = []
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.load_errors.append(str(exc))
            return
        rows = data.get("lessons") if isinstance(data, Mapping) else None
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            lesson = Lesson.from_payload(row)
            if lesson is not None:
                self._lessons.append(lesson)

    def save(self) -> None:
        document = {
            "version": 1,
            "lessons": [lesson.to_payload() for lesson in self._lessons],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def __len__(self) -> int:
        return len(self._lessons)

    def __iter__(self) -> Iterable[Lesson]:
        return iter(tuple(self._lessons))

    def add(self, lesson: Lesson) -> Lesson:
        """Remember a proposed lesson, replacing an identical fingerprint."""

        for index, existing in enumerate(self._lessons):
            if existing.fingerprint == lesson.fingerprint:
                self._lessons[index] = lesson
                return lesson
        self._lessons.append(lesson)
        return lesson

    def match(
        self,
        event_kind: str,
        payload: Mapping[str, Any],
        *,
        clock: Callable[[], float] = time.time,
    ) -> Lesson | None:
        """The active lesson for this failure, if any (guardrail 2: retired and
        stale lessons do not apply)."""

        fingerprint = lesson_fingerprint(event_kind, payload)
        now = clock()
        for lesson in self._lessons:
            if lesson.retired:
                continue
            if lesson.fingerprint != fingerprint:
                continue
            if lesson.last_applied_at and now - lesson.last_applied_at > (
                DEFAULT_STALE_DAYS * 86400
            ):
                continue
            return lesson
        return None

    def record_application(
        self, lesson_id: str, *, clock: Callable[[], float] = time.time
    ) -> bool:
        for lesson in self._lessons:
            if lesson.id != lesson_id:
                continue
            lesson.applied += 1
            lesson.last_applied_at = clock()
            return True
        return False

    def record_recurrence(
        self,
        lesson_id: str,
        *,
        retire_after: int = DEFAULT_RETIRE_AFTER,
    ) -> bool:
        """Count a recurrence of the same failure after the lesson applied.

        Returns True when the lesson was retired this call.
        """

        for lesson in self._lessons:
            if lesson.id != lesson_id:
                continue
            lesson.recurrences += 1
            if lesson.recurrences >= retire_after:
                lesson.retired = True
                return True
            return False
        return False

    def retire(self, lesson_id: str) -> bool:
        for lesson in self._lessons:
            if lesson.id == lesson_id:
                lesson.retired = True
                return True
        return False

    def active(self) -> tuple[Lesson, ...]:
        return tuple(lesson for lesson in self._lessons if not lesson.retired)

    def rows(self) -> list[dict[str, Any]]:
        return [lesson.to_payload() for lesson in self._lessons]
