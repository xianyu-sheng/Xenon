"""Chat turns leave a ledger record that session history can read back."""

from __future__ import annotations

from pathlib import Path

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.kernel.session_history import recent_task_lines
from xenon.ledger.conversation import (
    finish_conversation_turn,
    open_conversation_turn,
)
from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.store import Ledger


def _kinds(path: Path) -> list[EventKind]:
    return [event.kind for event in Ledger.open(path).read()]


def test_a_successful_chat_turn_is_a_valid_ledger(tmp_path: Path) -> None:
    turn = open_conversation_turn(
        tmp_path,
        objective="你好\n\n## 本轮执行边界",
        user_objective="你好",
        workspace=str(tmp_path),
        session_id="s1",
        model_ids=["m"],
    )
    assert turn is not None

    path = finish_conversation_turn(
        turn, answer="你好！", model_id="m", intent="chat"
    )

    assert path == turn.path
    ledger = Ledger.open(path)
    assert ledger.verify().valid
    kinds = _kinds(path)
    assert EventKind.TASK_STARTED in kinds
    assert EventKind.INTENT_CLASSIFIED in kinds
    assert EventKind.DELIVERED in kinds
    assert EventKind.FINALIZED in kinds
    started = ledger.read(EventKind.TASK_STARTED)[0].payload
    assert started["mode"] == "chat"
    assert started["session_id"] == "s1"
    assert started["workspace"] == str(tmp_path)
    assert started["user_objective"] == "你好"
    # Chat records live one level down; session history scans both levels.
    assert turn.path.parent.name == "chat"


def test_a_cancelled_turn_is_closed_not_left_open(tmp_path: Path) -> None:
    turn = open_conversation_turn(tmp_path, objective="long job", session_id="s1")
    assert turn is not None

    end = finish_conversation_turn(turn, cancelled=True)
    assert end is not None

    ledger = Ledger.open(end)
    assert ledger.verify().valid
    phases = [
        event.payload.get("to")
        for event in ledger.read(EventKind.STATE_TRANSITION)
    ]
    assert "aborted" in phases
    final = ledger.read(EventKind.FINALIZED)[0].payload
    assert final["cancelled"] is True
    assert final["evidence_status"] == "unverified"


def test_a_delegated_turn_is_marked_so_it_cannot_masquerade_as_an_answer(
    tmp_path: Path,
) -> None:
    turn = open_conversation_turn(tmp_path, objective="fix calc.py")
    assert turn is not None

    end = finish_conversation_turn(turn, cancelled=False, delegated=True)
    final = Ledger.open(end).read(EventKind.FINALIZED)[0].payload

    assert final["delegated"] is True
    assert final["evidence_status"] == "unverified"


def test_chat_turns_are_part_of_the_session_history(tmp_path: Path) -> None:
    """A follow-up can point at an explanation that never ran a tool.

    Excluding ``chat/`` from the history made a kernel turn answer "上一轮内容
    不存在" when the user asked about the first point of a previous chat
    answer: the prompt said earlier turns exist but could not show that one.
    """

    task = Ledger(tmp_path / "task.jsonl", "task")
    task.append(
        EventKind.TASK_STARTED,
        EventSource.USER,
        {
            "objective": "修复 calc.py",
            "workspace": str(tmp_path),
            "session_id": "s1",
        },
    )
    task.append(
        EventKind.FINALIZED, EventSource.RUNTIME, {"evidence_status": "verified"}
    )

    turn = open_conversation_turn(
        tmp_path, objective="解释一下缓存", workspace=str(tmp_path), session_id="s1"
    )
    assert turn is not None
    finish_conversation_turn(turn, answer="缓存分三层……", intent="chat")

    lines = recent_task_lines(
        tmp_path / "current.jsonl", session_id="s1", limit=5
    )

    assert any("修复 calc.py" in line for line in lines)
    assert any("解释一下缓存" in line and "缓存分三层" in line for line in lines)


def test_a_chat_ledger_can_be_read_back_by_id(tmp_path: Path) -> None:
    from xenon.kernel.session_history import task_record

    turn = open_conversation_turn(
        tmp_path, objective="解释一下缓存", workspace=str(tmp_path), session_id="s1"
    )
    assert turn is not None
    finish_conversation_turn(turn, answer="缓存分三层……")

    record = task_record(tmp_path / "current.jsonl", turn.task_id)

    assert record is not None
    assert record.objective == "解释一下缓存"
    assert record.answer == "缓存分三层……"


def test_report_and_replay_accept_a_minimal_chat_ledger(tmp_path: Path) -> None:
    from xenon.replay import metrics, timeline
    from xenon.report import build_report

    turn = open_conversation_turn(tmp_path, objective="hi", session_id="s1")
    assert turn is not None
    path = finish_conversation_turn(turn, answer="hello")
    assert path is not None

    report = build_report(path)
    assert report["integrity"]["valid"] is True
    assert report["status"]["evidence_status"] == "unverified"

    events = Ledger.open(path).read()
    assert timeline(events)  # every event has a renderable row
    assert metrics(events)["tool_calls"] == 0


def test_the_default_directory_honours_the_isolation_override(
    monkeypatch, tmp_path: Path
) -> None:
    from xenon.ledger.conversation import default_ledger_dir

    monkeypatch.setenv("XENON_LEDGER_DIR", str(tmp_path))

    assert default_ledger_dir() == tmp_path
