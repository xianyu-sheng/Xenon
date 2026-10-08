"""结构化回合归档（自动压缩的新思路）：确定性、事件化、可回放。"""

from __future__ import annotations


from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL
from xenon.session.archive import build_turn_archives, render_archive_block


def _events_for_turns(count: int) -> list[dict]:
    events: list[dict] = []
    for i in range(1, count + 1):
        events.append(
            {
                "type": "turn/user",
                "text": f"第{i}个任务：重构模块{i}",
                "intent": "debug",
                "level": 2,
                "continuation": False,
                "deferred": False,
                "at": i * 10,
            }
        )
        if i == 1:
            events.append(
                {
                    "type": "tool/result",
                    "tool": "write_file",
                    "success": True,
                    "paths": [f"mod{i}.py"],
                    "at": i * 10 + 1,
                }
            )
            events.append(
                {
                    "type": "tool/result",
                    "tool": "command",
                    "success": False,
                    "paths": [],
                    "error": "pytest 失败: 3 failed",
                    "at": i * 10 + 2,
                }
            )
            events.append(
                {
                    "type": "gate/opened",
                    "id": "g1",
                    "kind": "approval",
                    "reason": "等待确认",
                    "at": i * 10 + 3,
                }
            )
            events.append(
                {
                    "type": "gate/resolved",
                    "gate_id": "g1",
                    "outcome": "consumed",
                    "at": i * 10 + 4,
                }
            )
            events.append(
                {
                    "type": "goal/opened",
                    "objective": "重构模块1",
                    "at": i * 10 + 5,
                }
            )
            events.append(
                {
                    "type": "plan_mode/set",
                    "active": True,
                    "at": i * 10 + 6,
                }
            )
        events.append(
            {
                "type": "turn/assistant",
                "preview": f"第{i}轮结果：完成重构并通过验证",
                "at": i * 10 + 7,
            }
        )
    return events


def test_archives_fold_older_turns_into_structured_records():
    records = build_turn_archives(_events_for_turns(8), keep_last=2)

    assert len(records) == 6
    first = records[0]
    assert first["intent"] == "debug"
    assert "重构模块1" in first["objective"]
    assert first["tools"][0] == {
        "tool": "write_file",
        "success": True,
        "target": "mod1.py",
    }
    assert any("pytest 失败" in f for f in first["failures"])
    assert "mod1.py" in first["artifacts"]
    assert "门→consumed" in first["decisions"]
    assert "计划模式开" in first["decisions"]
    assert "完成重构" in first["outcome"]


def test_archive_block_is_bounded_and_mentions_failures():
    block = render_archive_block(build_turn_archives(_events_for_turns(8), keep_last=2))
    assert "早期回合归档" in block
    assert "T1" in block
    assert "✗" in block
    assert "pytest 失败" in block
    assert len(block) <= 1400


def test_keep_last_zero_archives_everything():
    records = build_turn_archives(_events_for_turns(3), keep_last=0)
    assert len(records) == 3


def test_no_turns_yields_no_archives():
    assert build_turn_archives([]) == []


# ── REPL 接线 ─────────────────────────────────────────────


def _make_repl(tmp_path, monkeypatch) -> REPL:
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    return REPL(registry=registry, streaming=False)


def test_auto_compact_feeds_deterministic_archives(monkeypatch, tmp_path):
    repl = _make_repl(tmp_path, monkeypatch)
    for event in _events_for_turns(8):
        del event["type"]
    for event in _events_for_turns(8):
        repl._record_event(event["type"], **{k: v for k, v in event.items() if k != "type"})

    captured: dict = {}
    monkeypatch.setattr(
        repl.ctx_mgr, "compact", lambda **kw: captured.update(kw) or "ok"
    )

    assert repl._auto_compact() is True
    assert "早期回合归档" in captured["summary"]
    events = repl._session_events.read()
    assert any(e["type"] == "compaction" for e in events)


def test_failed_tools_are_recorded_as_facts(monkeypatch, tmp_path):
    repl = _make_repl(tmp_path, monkeypatch)

    class _Step:
        action = "command"
        is_error = True
        action_input = {"command": "pytest"}
        error = "pytest 失败: 2 failed"

    class _Panel:
        steps = [_Step()]

    repl._track_session_files(_Panel())

    events = repl._session_events.read()
    tool_events = [e for e in events if e["type"] == "tool/result"]
    assert len(tool_events) == 1
    assert tool_events[0]["success"] is False
    assert "pytest 失败" in tool_events[0]["error"]
