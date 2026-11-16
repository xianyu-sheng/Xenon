"""The one boundary between a task and everything Xenon has learned.

Design: ``docs/design/self-evolution.md``.  Self-evolution is deterministic:
classification is ``LessonShape(event|subject)``, clustering groups by shape,
relevance is structural (the subject is bindable in this task's catalog), and
recall reads the stored records.  No LLM takes part in any learning decision;
if one is ever attached for wording, it produces a note, never state.

Before this facade the chain was spread over the runner (``_apply_lessons`` /
``_learn_from_ledger``), the store and the kernel proposer.  Every entry point
had to remember to call both halves, and the resumed-plan path was a second
copy of the same calls.  Now a task talks to :class:`LearningGateway` only:

    recall(task)  -> which lessons this task must carry (bounded, scored)
    apply(spec)   -> constraints + LESSON_APPLIED + application counters
    commit(ledger)-> LESSON_STORED / RETIRED / CLUSTERED / CONFIRMED

``recall`` is the only scoring that influences a task; ``commit`` only reads
the ledger the runtime already wrote.  Both are pure orchestration over
``xenon.harness.lessons`` — the store still owns persistence and the kernel
still owns proposal generation, so the layering does not move.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, replace
from typing import Any, Iterable

from xenon.harness.lessons import (
    design_debt_payload,
    record_recurrences,
    store_proposals,
    summarize_and_confirm,
)
from xenon.ledger.events import EventKind, EventSource

__all__ = ["LearnReport", "LearningGateway"]

#: How many lessons a single task may carry.  A constraint section that grows
#: without bound dilutes the prompt and costs cache prefix on every turn.
DEFAULT_RECALL_LIMIT = 3


@dataclass(frozen=True, slots=True)
class LearnReport:
    """What ``commit`` changed, for logs and tests (never for decisions)."""

    stored: tuple[str, ...] = ()
    retired: tuple[str, ...] = ()
    clustered: tuple[str, ...] = ()
    confirmed: tuple[tuple[str, str], ...] = ()


@dataclass(slots=True)
class LearningGateway:
    """Deterministic learning for exactly one store (``None`` = disabled).

    A host without a memory backend passes ``None`` and gets a gateway that
    does nothing: no lesson is carried, no event is written, no noise.  That
    is the same contract as the old ``lesson_store is None`` guards, now in
    one place.
    """

    store: Any = None

    @property
    def enabled(self) -> bool:
        return self.store is not None

    # ── input side: what the next task carries ─────────────────────

    def recall(
        self,
        *,
        available: Iterable[str] = (),
        limit: int = DEFAULT_RECALL_LIMIT,
    ) -> tuple[Any, ...]:
        """The active lessons relevant to a task with these catalog subjects.

        Bounded and ordered by the store's deterministic relevance score.  A
        broken store yields no lessons rather than failing the task: learning
        is advisory, the task is not.
        """

        if self.store is None:
            return ()
        try:
            # Decide from the signature, not from a thrown ``TypeError``: an
            # internal TypeError in a current store would otherwise be read as
            # "old store" and retried *without* the relevance filter, carrying
            # lessons the catalog cannot bind.
            parameters = inspect.signature(self.store.match_for_task).parameters
            kwargs: dict[str, Any] = {"limit": limit}
            if "available" in parameters:
                kwargs["available"] = available
            return tuple(self.store.match_for_task(**kwargs))
        except Exception:  # noqa: BLE001 — a broken store must not fail the task
            return ()

    def apply(self, spec: Any, ledger: Any, *, runtime: Any = None) -> Any:
        """Carry the recalled lessons into ``spec`` and record the applications.

        Shared by ``run_task`` and ``resume_task``: a resumed approved plan
        must learn exactly like a fresh run, or the chain quietly stops at the
        approval boundary.  ``runtime`` supplies the catalog's capability and
        tool names so a lesson about something this task cannot bind is not
        carried.
        """

        if self.store is None:
            return spec
        try:
            subjects = runtime.tool_subjects() if runtime is not None else ()
        except Exception:  # noqa: BLE001 — no catalog, keep every lesson
            subjects = ()
        applied = self.recall(available=subjects)
        if not applied:
            return spec
        spec = replace(
            spec,
            constraints=spec.constraints
            + tuple(
                f"[lesson {lesson.id}] {lesson.action_text}" for lesson in applied
            ),
        )
        already = self._applied_ids(ledger)
        # The event is written on every execution: the plan phase and the
        # resumed execution both genuinely carried the lesson, and the latest
        # application sequence is what bounds the recurrence scan.
        for lesson in applied:
            self._append(
                ledger,
                EventKind.LESSON_APPLIED,
                {
                    "lesson_id": lesson.id,
                    "action": lesson.action_text,
                    "kind": lesson.kind,
                },
            )
        record = getattr(self.store, "record_application", None)
        if callable(record):
            for lesson in applied:
                if lesson.id in already:
                    # ``apply`` runs twice for one user task (plan, then the
                    # approved resume); one task must not count as two
                    # applications.
                    continue
                try:
                    record(lesson.id)
                except Exception:  # noqa: BLE001 — counters are advisory
                    pass
        return spec

    @staticmethod
    def _applied_ids(ledger: Any) -> set[str]:
        try:
            events = ledger.read()
        except Exception:  # noqa: BLE001 — learning is advisory
            return set()
        ids = {
            str((getattr(event, "payload", {}) or {}).get("lesson_id") or "")
            for event in events
            if getattr(getattr(event, "kind", None), "value", "") == "lesson_applied"
        }
        ids.discard("")
        return ids

    @staticmethod
    def _append(ledger: Any, kind: Any, payload: dict[str, Any]) -> bool:
        """Best-effort ledger write for the *post-task* half.

        ``commit`` runs after a task has already finished; a disk error here
        must not turn that task into an error.
        """

        try:
            ledger.append(kind, EventSource.GATE, payload)
            return True
        except Exception:  # noqa: BLE001 — learning is advisory
            return False

    # ── output side: what this task taught Xenon ───────────────────

    def commit(self, ledger: Any) -> LearnReport:
        """Persist proposals, count recurrences, cluster and confirm rules.

        Runs after ledger verification, on the ledger the runtime already
        wrote.  Every step is deterministic and every failure is swallowed:
        a learning bug must never turn a finished task into an error.
        """

        if self.store is None:
            return LearnReport()
        stored = self._stored(ledger)
        retired = self._retired(ledger)
        clustered, confirmed = self._summarized(ledger)
        return LearnReport(
            stored=stored,
            retired=retired,
            clustered=clustered,
            confirmed=confirmed,
        )

    def _stored(self, ledger: Any) -> tuple[str, ...]:
        try:
            stored = store_proposals(ledger, self.store)
        except Exception:  # noqa: BLE001 — learning must not fail a task
            return ()
        for lesson_id in stored:
            self._append(
                ledger,
                EventKind.LESSON_STORED,
                {"lesson_id": lesson_id, "reason": "proposed during this task"},
            )
        return tuple(stored)

    def _retired(self, ledger: Any) -> tuple[str, ...]:
        try:
            retired = record_recurrences(ledger, self.store)
        except Exception:  # noqa: BLE001 — counting is advisory
            return ()
        for lesson_id in retired:
            self._append(
                ledger,
                EventKind.LESSON_RETIRED,
                {"lesson_id": lesson_id, "reason": "recurrence threshold reached"},
            )
            # A lesson that was applied and still recurred is no longer a
            # model problem: the guidance itself did not work.  Record a
            # design debt naming the family, so the answer is a mechanism
            # change (tool design / policy / verification), not another
            # prompt line.
            debt = design_debt_payload(self.store, lesson_id)
            if debt is not None:
                self._append(ledger, EventKind.DESIGN_DEBT_OPENED, debt)
        return tuple(retired)

    def _summarized(
        self, ledger: Any
    ) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
        try:
            created, advanced = summarize_and_confirm(ledger, self.store)
        except Exception:  # noqa: BLE001 — learning must not fail a task
            return (), ()
        for rule in created:
            self._append(
                ledger,
                EventKind.LESSON_CLUSTERED,
                {
                    "lesson_id": rule.id,
                    "shape": rule.shape,
                    "state": rule.state,
                    "derived_from": list(rule.derived_from),
                    "text": rule.action_text,
                },
            )
        for lesson_id, state in advanced:
            self._append(
                ledger,
                EventKind.LESSON_CONFIRMED,
                {
                    "lesson_id": lesson_id,
                    "state": state,
                    "reason": "applied and no same-shape failure recurred",
                },
            )
        return (
            tuple(rule.id for rule in created),
            tuple((lesson_id, state) for lesson_id, state in advanced),
        )
