"""Lesson store: slice 1 of self-evolution (docs/design/self-evolution.md).

Covers the storage + fingerprint + retirement semantics.  The proposer (slice
2), the applier (slice 3) and the PITFALLS.md projection (slice 4) wire the
same store; this file pins the rules they all rely on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xenon.harness.lessons import LessonStore
from xenon.kernel.lessons import (
    Lesson,
    lesson_fingerprint,
)
from xenon.ledger.events import EventSource


def _lesson(**kwargs) -> Lesson:
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


# ── the store ─────────────────────────────────────────────────────


def test_an_empty_store_matches_nothing(tmp_path: Path) -> None:
    store = LessonStore(tmp_path / "lessons.yaml")

    assert len(store) == 0
    assert store.match("debt_opened", {"kind": "requirement_met"}) is None


def test_a_matching_fingerprint_is_found(tmp_path: Path) -> None:
    store = LessonStore(tmp_path / "lessons.yaml")
    lesson = _lesson()
    store.add(lesson)

    found = store.match(
        "debt_opened",
        {"kind": "requirement_met", "target": "x", "reason": "no receipt"},
    )

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

    # Same fingerprint as _lesson, applied 31 days ago: stale → no match.
    assert (
        store.match(
            "debt_opened",
            {"kind": "requirement_met", "target": "x"},
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


# ── store edge cases the first batch missed ───────────────────────


def test_a_non_list_document_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "lessons.yaml"
    path.write_text(json.dumps({"lessons": "not-a-list"}), encoding="utf-8")

    assert len(LessonStore(path)) == 0


def test_non_mapping_rows_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "lessons.yaml"
    path.write_text(
        json.dumps(
            {
                "lessons": [
                    "not-a-mapping",
                    {"id": "ok", "kind": "pitfall", "trigger_event": "x",
                     "fingerprint": "f", "action_text": "a"},
                ]
            }
        ),
        encoding="utf-8",
    )

    store = LessonStore(path)
    assert len(store) == 1
    assert next(iter(store)).id == "ok"


def test_adding_replaces_the_same_fingerprint(tmp_path: Path) -> None:
    store = LessonStore(tmp_path / "lessons.yaml")
    first = _lesson(action_text="old")
    second = _lesson(action_text="new")
    store.add(first)
    store.add(second)

    assert len(store) == 1
    assert next(iter(store)).action_text == "new"


def test_operations_on_an_unknown_lesson_are_noops(tmp_path: Path) -> None:
    store = LessonStore(tmp_path / "lessons.yaml")

    assert store.record_application("nope") is False
    assert store.record_recurrence("nope") is False
    assert store.retire("nope") is False
    assert store.match("debt_opened", {"kind": "x"}) is None
    assert store.active() == ()


def test_operations_on_a_non_first_lesson(tmp_path: Path) -> None:
    """record_application / record_recurrence / retire skip the *other* lesson
    before finding the target (the continue branches)."""

    store = LessonStore(tmp_path / "lessons.yaml")
    first = _lesson(id="L-0001", fingerprint="fp-1")
    second = _lesson(id="L-0002", fingerprint="fp-2")
    store.add(first)
    store.add(second)

    assert store.record_application("L-0002") is True
    assert second.applied == 1 and first.applied == 0
    assert store.record_recurrence("L-0002", retire_after=3) is False
    assert second.recurrences == 1
    assert store.retire("L-0002") is True
    assert second.retired is True and first.retired is False


def test_retire_and_rows_expose_state(tmp_path: Path) -> None:
    store = LessonStore(tmp_path / "lessons.yaml")
    lesson = _lesson()
    store.add(lesson)

    assert store.retire(lesson.id) is True
    assert store.active() == ()
    assert store.rows()[0]["retired"] is True


def test_application_then_save_then_reload(tmp_path: Path) -> None:
    path = tmp_path / "lessons.yaml"
    store = LessonStore(path)
    lesson = _lesson()
    store.add(lesson)
    store.record_application(lesson.id)
    store.save()

    reloaded = LessonStore(path)

    assert next(iter(reloaded)).applied == 1


# ── slice 3: task application ─────────────────────────────────────


def test_match_for_task_is_bounded_and_recent_first(tmp_path: Path) -> None:
    store = LessonStore(tmp_path / "lessons.yaml")
    for index in range(5):
        store.add(_lesson(
            id=f"L-{index:04d}",
            fingerprint=f"fp-{index}",
            last_applied_at=float(index),
        ))

    selected = store.match_for_task(limit=3)

    assert len(selected) == 3
    # Most recently applied first.
    assert [lesson.id for lesson in selected] == ["L-0004", "L-0003", "L-0002"]


def test_match_for_task_excludes_parameter_lessons(tmp_path: Path) -> None:
    store = LessonStore(tmp_path / "lessons.yaml")
    store.add(_lesson(id="L-0001", kind="pitfall", fingerprint="fp-1"))
    store.add(_lesson(id="L-0002", kind="parameter", fingerprint="fp-2"))
    store.add(_lesson(id="L-0003", kind="preference", fingerprint="fp-3"))

    selected = store.match_for_task()

    assert {lesson.kind for lesson in selected} == {"pitfall", "preference"}


def test_match_for_task_excludes_retired_lessons(tmp_path: Path) -> None:
    store = LessonStore(tmp_path / "lessons.yaml")
    store.add(_lesson(id="L-0001", fingerprint="fp-1", retired=True))
    store.add(_lesson(id="L-0002", fingerprint="fp-2"))

    assert [lesson.id for lesson in store.match_for_task()] == ["L-0002"]


def test_recurrences_are_counted_and_retired(tmp_path: Path) -> None:
    from xenon.harness.lessons import record_recurrences
    from xenon.ledger.events import EventKind
    from xenon.ledger.store import Ledger

    store = LessonStore(tmp_path / "lessons.yaml")
    lesson = _lesson()
    store.add(lesson)
    ledger = Ledger.in_memory("t1")
    # Two more failures with the same fingerprint.
    for _ in range(2):
        ledger.append(
            EventKind.DEBT_OPENED,
            EventSource.GATE,
            {"kind": "requirement_met", "target": "x", "reason": "no receipt"},
        )

    retired = record_recurrences(ledger, store)

    assert lesson.recurrences == 2
    # Threshold is 3 in the design; a manual retire() still works.
    assert retired == []
    assert store.retire(lesson.id) is True
    assert lesson.retired is True


# ── slice 3: the runner wires lessons into tasks ──────────────────


def _fake_client() -> Any:
    class FakeClient:
        def __call__(self, model_id, messages, **kwargs):
            return json.dumps({"action": "finish", "answer": "done"})

    return FakeClient()


def test_run_task_injects_bounded_lessons_as_constraints(tmp_path: Path) -> None:
    from tests.harness.test_exec import _repo
    from xenon.harness.llm import ModelConfig, build_llm_loop_strategy
    from xenon.harness.runner import run_task
    from xenon.kernel.strategies import LoopProfile
    from xenon.ledger.events import EventKind
    from xenon.ledger.store import Ledger

    _repo(tmp_path)
    repo_dir = tmp_path
    store = LessonStore(tmp_path / "lessons.yaml")
    store.add(_lesson(id="L-0001", fingerprint="fp-1", last_applied_at=1.0))
    store.add(_lesson(id="L-0002", fingerprint="fp-2", last_applied_at=2.0))
    store.add(_lesson(id="L-0003", fingerprint="fp-3", last_applied_at=3.0))
    store.add(_lesson(id="L-0004", fingerprint="fp-4", last_applied_at=4.0))

    strategy = build_llm_loop_strategy(
        ModelConfig(model_id="deepseek/deepseek-v4-flash"),
        LoopProfile(),
        client=_fake_client(),
    )
    run = run_task(
        "read README and report",
        strategy=strategy,
        workspace=repo_dir,
        ledger_dir=tmp_path / "ledger",
        task_id="lesson-applied",
        max_steps=3,
        lesson_store=store,
    )

    events = Ledger(run.ledger_path, "lesson-applied").read()
    applied = [
        event for event in events if event.kind is EventKind.LESSON_APPLIED
    ]
    assert len(applied) == 3, "only the bounded subset is applied"
    assert [e.payload["lesson_id"] for e in applied] == [
        "L-0004", "L-0003", "L-0002",
    ]
    assert applied[0].payload["action"] == "不要声称 test_passed"


def test_retired_lessons_are_not_applied(tmp_path: Path) -> None:
    from tests.harness.test_exec import _repo
    from xenon.harness.llm import ModelConfig, build_llm_loop_strategy
    from xenon.harness.runner import run_task
    from xenon.kernel.strategies import LoopProfile
    from xenon.ledger.events import EventKind
    from xenon.ledger.store import Ledger

    _repo(tmp_path)
    repo_dir = tmp_path
    store = LessonStore(tmp_path / "lessons.yaml")
    store.add(_lesson(id="L-0001", fingerprint="fp-1"))
    store.add(_lesson(id="L-0002", fingerprint="fp-2", retired=True))

    strategy = build_llm_loop_strategy(
        ModelConfig(model_id="deepseek/deepseek-v4-flash"),
        LoopProfile(),
        client=_fake_client(),
    )
    run = run_task(
        "read README and report",
        strategy=strategy,
        workspace=repo_dir,
        ledger_dir=tmp_path / "ledger",
        task_id="lesson-no-retired",
        max_steps=3,
        lesson_store=store,
    )

    applied = [
        event.payload["lesson_id"]
        for event in Ledger(run.ledger_path, "lesson-no-retired").read()
        if event.kind is EventKind.LESSON_APPLIED
    ]
    assert "L-0002" not in applied


# ── slice 3: failure-path guards ─────────────────────────────────


def test_record_recurrences_ignores_non_lesson_stores() -> None:
    from xenon.harness.lessons import record_recurrences
    from xenon.ledger.store import Ledger

    ledger = Ledger.in_memory("t1")
    assert record_recurrences(ledger, "not-a-store") == []


def test_record_recurrences_skips_non_veto_policy_and_approved() -> None:
    from xenon.harness.lessons import record_recurrences
    from xenon.ledger.events import EventKind
    from xenon.ledger.store import Ledger

    store = LessonStore(Path("nonexistent.yaml"))
    store._lessons = []
    ledger = Ledger.in_memory("t2")
    ledger.append(EventKind.POLICY_DECISION, EventSource.GATE, {"kind": "allow"})
    ledger.append(EventKind.USER_APPROVAL, EventSource.USER, {"approved": True})

    assert record_recurrences(ledger, store) == []


def test_a_broken_lesson_store_does_not_fail_the_task(tmp_path: Path) -> None:
    from tests.harness.test_exec import _repo
    from xenon.harness.llm import ModelConfig, build_llm_loop_strategy
    from xenon.harness.runner import run_task
    from xenon.kernel.strategies import LoopProfile

    _repo(tmp_path)
    store = LessonStore(tmp_path / "lessons.yaml")

    def boom(*args, **kwargs):
        raise RuntimeError("store broke")

    store.match_for_task = boom
    store.record_application = boom
    strategy = build_llm_loop_strategy(
        ModelConfig(model_id="deepseek/deepseek-v4-flash"),
        LoopProfile(),
        client=_fake_client(),
    )
    run = run_task(
        "read README and report",
        strategy=strategy,
        workspace=tmp_path,
        ledger_dir=tmp_path / "ledger",
        task_id="lesson-broken-store",
        max_steps=3,
        lesson_store=store,
    )

    assert run.result is not None


def test_recurrence_retires_and_runner_writes_the_event(tmp_path: Path) -> None:
    """Full loop: a lesson applied, the same failure recurs, the lesson is
    retired and LESSON_RETIRED lands in the task's ledger."""

    from tests.harness.test_exec import _repo
    from xenon.harness.llm import ModelConfig, build_llm_loop_strategy
    from xenon.harness.runner import run_task
    from xenon.kernel.strategies import LoopProfile
    from xenon.ledger.store import Ledger

    _repo(tmp_path)
    # A task that edits then claims an unverifiable effect opens a debt with a
    # deterministic shape; the lesson's fingerprint is that same shape.
    class DebtClient:
        """First call edits, second call finishes with an unverifiable claim."""

        def __init__(self):
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
                                {"path": "calc/add.py",
                                 "content": "def add(a, b):" + chr(10) + "    return a + b" + chr(10)}
                            ]
                        },
                    }
                )
            return json.dumps(
                {
                    "action": "finish",
                    "answer": "done",
                    "claims": [
                        {"id": "c1", "kind": "tool_succeeded", "target": "echo hi"}
                    ],
                }
            )

    client = DebtClient()
    # First run the task without a store to learn the exact debt shape, then
    # store the matching lesson.
    from xenon.kernel.lesson_proposer import propose_lesson

    store = LessonStore(tmp_path / "lessons.yaml")
    # Pre-seed a lesson whose fingerprint matches the canonical debt shape of
    # an unverifiable claim, at recurrence count 2 (one more recurs → retire).
    lesson = _lesson(id="L-0001", fingerprint="fp-x", recurrences=2, retired=False)
    # The canonical debt shape an unverifiable claim actually produces:
    # obligation kind is requirement_met, target is empty, reason is the
    # controller's fixed string.
    lesson.fingerprint = propose_lesson(
        "debt_opened",
        {
            "kind": "requirement_met",
            "target": "",
            "reason": "receipt status is not indexed as a claim fact",
        },
    ).fingerprint
    store.add(lesson)

    strategy = build_llm_loop_strategy(
        ModelConfig(model_id="deepseek/deepseek-v4-flash"),
        LoopProfile(),
        client=client,
    )
    run = run_task(
        "edit calc/add.py then run a test",
        strategy=strategy,
        workspace=tmp_path,
        ledger_dir=tmp_path / "ledger",
        task_id="lesson-retire",
        max_steps=4,
        lesson_store=store,
    )

    events = Ledger(run.ledger_path, "lesson-retire").read()
    kinds = [event.kind.value for event in events]
    assert "lesson_retired" in kinds, kinds
    assert lesson.retired is True


def test_a_broken_proposer_in_recurrence_scan_is_skipped(tmp_path: Path) -> None:
    from xenon.harness.lessons import record_recurrences
    from xenon.ledger.events import EventKind
    from xenon.ledger.store import Ledger

    store = LessonStore(tmp_path / "lessons.yaml")
    ledger = Ledger.in_memory("t3")
    ledger.append(
        EventKind.DEBT_OPENED, EventSource.GATE, {"kind": "requirement_met"}
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


def test_a_broken_recurrence_counter_does_not_fail_the_task(tmp_path: Path) -> None:
    import xenon.harness.runner as runner_module
    from tests.harness.test_exec import _repo
    from xenon.harness.llm import ModelConfig, build_llm_loop_strategy
    from xenon.harness.runner import run_task
    from xenon.kernel.strategies import LoopProfile

    _repo(tmp_path)
    store = LessonStore(tmp_path / "lessons.yaml")

    def boom(*args, **kwargs):
        raise RuntimeError("counter broke")

    original = runner_module.record_recurrences
    runner_module.record_recurrences = boom
    try:
        strategy = build_llm_loop_strategy(
            ModelConfig(model_id="deepseek/deepseek-v4-flash"),
            LoopProfile(),
            client=_fake_client(),
        )
        run = run_task(
            "read README and report",
            strategy=strategy,
            workspace=tmp_path,
            ledger_dir=tmp_path / "ledger",
            task_id="lesson-broken-counter",
            max_steps=3,
            lesson_store=store,
        )
    finally:
        runner_module.record_recurrences = original

    assert run.result is not None


def test_a_broken_application_counter_does_not_fail_the_task(tmp_path: Path) -> None:
    from tests.harness.test_exec import _repo
    from xenon.harness.llm import ModelConfig, build_llm_loop_strategy
    from xenon.harness.runner import run_task
    from xenon.kernel.strategies import LoopProfile

    _repo(tmp_path)
    store = LessonStore(tmp_path / "lessons.yaml")
    store.add(_lesson(id="L-0001", fingerprint="fp-1"))

    def boom(*args, **kwargs):
        raise RuntimeError("counter broke")

    store.record_application = boom
    strategy = build_llm_loop_strategy(
        ModelConfig(model_id="deepseek/deepseek-v4-flash"),
        LoopProfile(),
        client=_fake_client(),
    )
    run = run_task(
        "read README and report",
        strategy=strategy,
        workspace=tmp_path,
        ledger_dir=tmp_path / "ledger",
        task_id="lesson-broken-app-counter",
        max_steps=3,
        lesson_store=store,
    )

    assert run.result is not None
    # The constraint was still injected even though the counter broke.
    from xenon.ledger.events import EventKind
    from xenon.ledger.store import Ledger

    applied = [
        e for e in Ledger(run.ledger_path, "lesson-broken-app-counter").read()
        if e.kind is EventKind.LESSON_APPLIED
    ]
    assert len(applied) == 1


def test_match_for_task_zero_limit_disables(tmp_path: Path) -> None:
    """#369 review: limit=0 means no lessons, not a minimum of one."""
    store = LessonStore(tmp_path / "lessons.yaml")
    store.add(_lesson(id="L-0001", kind="pitfall", fingerprint="fp-1"))

    assert store.match_for_task(limit=0) == ()
    assert store.match_for_task(limit=-1) == ()
