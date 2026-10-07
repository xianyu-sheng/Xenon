"""Additive session event log + projection (P1 core)."""

from __future__ import annotations

from xenon.repl.execution_policy import ExecutionPolicy
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL
from xenon.repl.turn_contract import build_turn_contract
from xenon.session.events import SessionEventLog
from xenon.session.projection import project


# ── 事件日志 ───────────────────────────────────────────────


def test_append_and_read_roundtrip(tmp_path):
    log = SessionEventLog("s1", directory=tmp_path)
    event_id = log.append("turn/user", text="你好")
    assert event_id
    log.append("tool/result", tool="write_file", success=True, paths=["a.py"])

    events = log.read()
    assert [e["type"] for e in events] == ["turn/user", "tool/result"]
    assert events[0]["id"] == event_id
    assert events[1]["paths"] == ["a.py"]


def test_corrupt_lines_are_skipped(tmp_path):
    path = tmp_path / "s1.jsonl"
    path.write_text(
        '{"type":"turn/user","text":"ok"}\nnot json\n\n{"broken": true}\n',
        encoding="utf-8",
    )
    events = SessionEventLog("s1", directory=tmp_path).read()
    assert [e["type"] for e in events] == ["turn/user"]


def test_disabled_log_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_SESSION_EVENTS", "0")
    log = SessionEventLog("s1", directory=tmp_path)
    assert log.append("turn/user", text="x") is None
    assert not list(tmp_path.glob("*.jsonl"))


def test_append_failure_is_tolerated(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    log = SessionEventLog("s1", directory=blocker)
    assert log.append("turn/user", text="x") is None
    assert log.read() == []


# ── 投影 ───────────────────────────────────────────────────


def test_projection_folds_goals_gates_artifacts():
    events = [
        {"type": "goal/opened", "objective": "修复快排脚本", "at": 1},
        {"type": "gate/opened", "id": "g1", "kind": "approval", "reason": "等待确认", "at": 2},
        {"type": "tool/result", "tool": "write_file", "success": True, "paths": ["C:/x.py"], "at": 3},
        {"type": "tool/result", "tool": "write_file", "success": False, "paths": ["C:/bad.py"], "at": 4},
        {"type": "turn/user", "text": "写一下", "at": 5},
        {"type": "turn/assistant", "preview": "已写入", "at": 6},
        {"type": "goal/paused", "at": 7},
        {"type": "goal/opened", "objective": "新任务", "at": 8},
        {"type": "gate/resolved", "gate_id": "g1", "outcome": "consumed", "at": 9},
    ]
    view = project(events)

    assert view.active_goal is not None
    assert view.active_goal.objective == "新任务"
    assert [g.status for g in view.goals] == ["paused", "active"]
    assert view.open_gates == []
    assert [a.path for a in view.artifacts] == ["C:/x.py"]
    assert view.recent_users == ["写一下"]
    assert view.assistant_previews == ["已写入"]


def test_projection_keeps_unresolved_gates_open():
    view = project(
        [
            {"type": "gate/opened", "id": "g1", "kind": "continuation", "reason": "续写"},
            {"type": "gate/opened", "id": "g2", "kind": "approval", "reason": "写盘"},
            {"type": "gate/resolved", "gate_id": "g1", "outcome": "stale"},
        ]
    )
    assert [g.gate_id for g in view.open_gates] == ["g2"]


def test_projection_pauses_previous_goal_on_a_new_open():
    view = project(
        [
            {"type": "goal/opened", "objective": "任务甲", "at": 1},
            {"type": "goal/opened", "objective": "任务乙", "at": 2},
        ]
    )
    assert [g.status for g in view.goals] == ["paused", "active"]
    assert view.active_goal is not None
    assert view.active_goal.objective == "任务乙"


# ── REPL 接线 ─────────────────────────────────────────────


def _make_repl(tmp_path, monkeypatch) -> REPL:
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    return REPL(registry=registry, streaming=False)


def test_deferred_turn_records_gate_and_assistant_events(tmp_path, monkeypatch):
    repl = _make_repl(tmp_path, monkeypatch)
    contract = build_turn_contract("先给我代码，我让你写你再写")
    repl._update_pending_action(
        mode="plan-execute",
        contract=contract,
        policy=ExecutionPolicy(contract.level, "test"),
    )

    assert repl._pending_action is not None
    assert repl._pending_action.gate_id
    events = repl._session_events.read()
    types = [e["type"] for e in events]
    assert "turn/assistant" in types
    assert "gate/opened" in types
    gate = next(e for e in events if e["type"] == "gate/opened")
    assert gate["kind"] == "approval"
    assert gate["id"] == repl._pending_action.gate_id


def test_record_event_is_failure_tolerant(tmp_path, monkeypatch):
    repl = _make_repl(tmp_path, monkeypatch)
    repl._session_events = None
    assert repl._record_event("turn/user", text="x") is None
