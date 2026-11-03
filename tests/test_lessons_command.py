"""``/lessons`` command: list, and the explicit PITFALLS.md projection."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def _store(tmp_path: Path):
    from xenon.harness.lessons import LessonStore
    from xenon.kernel.lessons import Lesson

    store = LessonStore(tmp_path / ".xenon" / "lessons.yaml")
    store.add(
        Lesson(
            id="L-0001", kind="pitfall", trigger_event="debt_opened",
            fingerprint="f1", action_text="不要凭空声称", applied=2,
            recurrences=1, counterfactual="回执机制改变",
        )
    )
    store.add(
        Lesson(
            id="L-0002", kind="parameter", trigger_event="loop_detected",
            fingerprint="f2", action_text="降低调用预算", applied=0,
            recurrences=0,
        )
    )
    store.add(
        Lesson(
            id="L-0003", kind="preference", trigger_event="user_approval",
            fingerprint="f3", action_text="避免该调用", retired=True,
        )
    )
    store.save()
    return store


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch):
    """Point LessonStore's home at the tmp directory so the real ~/.xenon is
    never touched."""

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))


def test_lessons_lists_counters_and_states(tmp_path: Path) -> None:
    from xenon.repl.command_groups.lessons import _cmd_lessons

    _store(tmp_path)
    text = _cmd_lessons(args="", session_state={})

    assert "L-0001" in text and "applied=2" in text and "recur=1" in text
    assert "active" in text
    assert "L-0003" in text and "retired" in text


def test_lessons_write_projects_pitfalls(tmp_path: Path) -> None:
    from xenon.repl.command_groups.lessons import _cmd_lessons

    _store(tmp_path)
    text = _cmd_lessons(args="write", session_state={"_repl": SimpleNamespace(
        project_ctx=SimpleNamespace(root=tmp_path),
    )})

    target = tmp_path / ".xenon" / "PITFALLS.md"
    assert target.is_file()
    body = target.read_text(encoding="utf-8")
    assert "L-0001" in body
    assert "不要凭空声称" in body
    assert "失效条件" in body
    assert "L-0002" not in body  # parameter lessons stay out of the repo file
    assert "L-0003" not in body  # retired lessons stay out
    assert "已写入" in text


def test_lessons_without_any_lesson_is_informative(tmp_path: Path) -> None:
    from xenon.repl.command_groups.lessons import _cmd_lessons

    text = _cmd_lessons(args="", session_state={})

    assert "当前没有教训" in text


def test_lessons_stale_state_and_clear_message(tmp_path: Path) -> None:
    from xenon.harness.lessons import LessonStore
    from xenon.kernel.lessons import Lesson
    from xenon.repl.command_groups.lessons import _cmd_lessons

    store = LessonStore(tmp_path / ".xenon" / "lessons.yaml")
    store.add(
        Lesson(
            id="L-STALE", kind="pitfall", trigger_event="debt_opened",
            fingerprint="fs", action_text="x", last_applied_at=1.0,
        )
    )
    store.save()

    text = _cmd_lessons(args="", session_state={})
    assert "stale" in text

    cleared = _cmd_lessons(args="clear", session_state={})
    assert "lessons.yaml" in cleared
