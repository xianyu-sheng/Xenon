"""The ``LearningGateway`` boundary: recall/apply in, commit out.

The facade exists so no entry point can learn on one path and silently skip
learning on another.  These tests pin the façade's contract itself; the
end-to-end proposal -> stored -> applied -> retired chain stays in
``tests/test_lessons.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xenon.harness.learning import LearningGateway, LearnReport
from xenon.harness.lessons import LessonStore
from xenon.kernel.contracts import TaskSpec
from xenon.kernel.lessons import Lesson, lesson_fingerprint
from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.store import Ledger


def _store(tmp_path: Path) -> LessonStore:
    from xenon.memory import MemoryBackendRegistry, MemoryService

    return LessonStore(MemoryService(MemoryBackendRegistry(tmp_path / "memory")))


def _lesson(
    lesson_id: str,
    *,
    shape: str,
    fingerprint: str,
    retired: bool = False,
    applied: int = 0,
) -> Lesson:
    return Lesson(
        id=lesson_id,
        kind="rule" if lesson_id.startswith("rule-") else "pitfall",
        trigger_event=shape.split("|", 1)[0],
        fingerprint=fingerprint,
        action_text=f"avoid {shape}",
        shape=shape,
        retired=retired,
        applied=applied,
    )


def _spec(*constraints: str) -> TaskSpec:
    return TaskSpec(
        task_id="t1",
        objective="read README",
        workspace=".",
        constraints=tuple(constraints),
    )


class _Runtime:
    def __init__(self, subjects: set[str]) -> None:
        self._subjects = subjects

    def tool_subjects(self) -> set[str]:
        return set(self._subjects)


# ── disabled ──────────────────────────────────────────────────────


def test_a_gateway_without_a_store_is_a_no_op() -> None:
    gateway = LearningGateway()
    ledger = Ledger(None, "t1")
    spec = _spec("existing")

    assert gateway.enabled is False
    assert gateway.apply(spec, ledger) is spec
    assert gateway.commit(ledger) == LearnReport()
    assert ledger.read() == ()


# ── recall / scoring ─────────────────────────────────────────────


def test_recall_scores_relevance_from_the_catalog(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_lesson("L-shell", shape="policy_decision|shell", fingerprint="fp-1"))
    store.add(_lesson("L-vcs", shape="policy_decision|vcs", fingerprint="fp-2"))
    gateway = LearningGateway(store)

    assert [lesson.id for lesson in gateway.recall(available={"shell"})] == [
        "L-shell"
    ]
    # No catalog information: fail open, never silently drop a safety lesson.
    assert {lesson.id for lesson in gateway.recall()} == {"L-shell", "L-vcs"}


def test_recall_is_bounded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(5):
        store.add(
            _lesson(f"L-{index}", shape="debt_opened|requirement_met", fingerprint=f"f{index}")
        )

    assert len(LearningGateway(store).recall(limit=2)) == 2
    assert LearningGateway(store).recall(limit=0) == ()


# ── apply ────────────────────────────────────────────────────────


def test_apply_appends_constraints_and_records_applications(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.add(_lesson("L-shell", shape="policy_decision|shell", fingerprint="fp-1"))
    store.add(_lesson("L-read", shape="policy_decision|read", fingerprint="fp-2"))
    gateway = LearningGateway(store)
    ledger = Ledger(tmp_path / "t.jsonl", "t1")

    spec = gateway.apply(_spec("keep"), ledger, runtime=_Runtime({"shell"}))

    assert spec.constraints[0] == "keep"
    assert len(spec.constraints) == 2
    assert "[lesson L-shell]" in spec.constraints[1]
    applied = [
        event.payload["lesson_id"]
        for event in ledger.read()
        if event.kind is EventKind.LESSON_APPLIED
    ]
    assert applied == ["L-shell"]
    assert store.get("L-shell").applied == 1
    assert store.get("L-read").applied == 0


def test_apply_with_no_relevant_lesson_leaves_the_spec_untouched(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.add(_lesson("L-vcs", shape="policy_decision|vcs", fingerprint="fp-1"))
    ledger = Ledger(tmp_path / "t.jsonl", "t1")

    spec = LearningGateway(store).apply(_spec(), ledger, runtime=_Runtime({"read"}))

    assert spec.constraints == ()
    assert ledger.read() == ()


def test_apply_survives_a_broken_store() -> None:
    class BrokenStore:
        def match_for_task(self, **kwargs: Any) -> Any:
            raise RuntimeError("store broke")

    ledger = Ledger(None, "t1")
    spec = _spec()

    assert LearningGateway(BrokenStore()).apply(spec, ledger) is spec
    assert ledger.read() == ()


# ── commit: store ────────────────────────────────────────────────


def _proposal_event_payload() -> dict[str, Any]:
    from xenon.kernel.lesson_proposer import propose_lesson

    proposal = propose_lesson(
        "user_approval",
        {"approved": False, "tool": "shell", "reason": "not now"},
        source_task="t1",
    )
    assert proposal is not None
    return {
        "kind": proposal.kind,
        "trigger_event": proposal.trigger_event,
        "fingerprint": proposal.fingerprint,
        "action_text": proposal.action_text,
        "source_task": proposal.source_task,
        "counterfactual": proposal.counterfactual,
        "shape": proposal.shape,
    }


def test_commit_stores_a_proposal_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gateway = LearningGateway(store)
    ledger = Ledger(tmp_path / "t.jsonl", "t1")
    ledger.append(
        EventKind.LESSON_PROPOSED,
        EventSource.RUNTIME,
        _proposal_event_payload(),
    )

    first = gateway.commit(ledger)
    second = gateway.commit(ledger)

    assert len(first.stored) == 1
    assert second.stored == ()
    stored = [
        event for event in ledger.read() if event.kind is EventKind.LESSON_STORED
    ]
    assert len(stored) == 1
    assert stored[0].payload["lesson_id"] == first.stored[0]


# ── commit: recurrence -> retirement + design debt ──────────────


def test_commit_retires_a_lesson_that_keeps_recurring_and_opens_debt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    fingerprint = lesson_fingerprint(
        "policy_decision",
        {"kind": "veto", "capability": "shell", "reason": "denied"},
    )
    store.add(
        _lesson("L-shell", shape="policy_decision|shell", fingerprint=fingerprint)
    )
    ledger = Ledger(tmp_path / "t.jsonl", "t1")
    ledger.append(
        EventKind.LESSON_APPLIED, EventSource.GATE, {"lesson_id": "L-shell"}
    )
    for _ in range(3):
        ledger.append(
            EventKind.POLICY_DECISION,
            EventSource.GATE,
            {"kind": "veto", "capability": "shell", "reason": "denied"},
        )

    report = LearningGateway(store).commit(ledger)

    assert report.retired == ("L-shell",)
    kinds = [event.kind for event in ledger.read()]
    assert EventKind.LESSON_RETIRED in kinds
    assert EventKind.DESIGN_DEBT_OPENED in kinds
    assert store.get("L-shell").retired is True


# ── commit: summarize + confirm ─────────────────────────────────


def test_commit_clusters_a_family_and_confirms_the_rule(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(2):
        store.add(
            _lesson(
                f"L-{index}",
                shape="policy_decision|shell",
                fingerprint=f"fp-{index}",
            )
        )
    gateway = LearningGateway(store)
    first = Ledger(tmp_path / "t1.jsonl", "t1")

    report = gateway.commit(first)

    assert len(report.clustered) == 1
    rule_id = report.clustered[0]
    assert any(
        event.kind is EventKind.LESSON_CLUSTERED for event in first.read()
    )

    # A later task carried the rule (two applications) and nothing of its
    # family failed: the hypothesis becomes active.
    store.record_application(rule_id)
    store.record_application(rule_id)
    second = Ledger(tmp_path / "t2.jsonl", "t2")
    second.append(
        EventKind.LESSON_APPLIED, EventSource.GATE, {"lesson_id": rule_id}
    )

    report = gateway.commit(second)

    assert report.confirmed == ((rule_id, "active"),)
    assert any(
        event.kind is EventKind.LESSON_CONFIRMED for event in second.read()
    )
    assert store.get(rule_id).state == "active"


def test_commit_survives_a_ledger_that_refuses_writes(tmp_path: Path) -> None:
    """A finished task must not become an error because a write failed."""

    store = _store(tmp_path)
    for index in range(2):
        store.add(
            _lesson(
                f"L-{index}",
                shape="policy_decision|shell",
                fingerprint=f"fp-{index}",
            )
        )

    class ReadOnlyLedger:
        def read(self) -> tuple[Any, ...]:
            return ()

        def append(self, *args: Any, **kwargs: Any) -> Any:
            raise OSError("disk full")

    report = LearningGateway(store).commit(ReadOnlyLedger())

    assert len(report.clustered) == 1
    assert store.get(report.clustered[0]).kind == "rule"


def test_commit_swallows_a_broken_store() -> None:
    class BrokenStore:
        def summarize(self, **kwargs: Any) -> Any:
            raise RuntimeError("cluster broke")

    ledger = Ledger(None, "t1")

    assert LearningGateway(BrokenStore()).commit(ledger) == LearnReport()
