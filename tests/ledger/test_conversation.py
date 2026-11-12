"""Chat turns leave a ledger record without crowding out task history."""

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
    # Chat records live one level down: session history scans only the top.
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


def test_chat_turns_do_not_crowd_out_real_task_history(tmp_path: Path) -> None:
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
        tmp_path, objective="你好", workspace=str(tmp_path), session_id="s1"
    )
    assert turn is not None
    finish_conversation_turn(turn, answer="你好！")

    lines = recent_task_lines(tmp_path / "current.jsonl", session_id="s1")

    assert any("修复 calc.py" in line for line in lines)
    assert not any("你好" in line for line in lines)


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
