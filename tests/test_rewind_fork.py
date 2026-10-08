"""C：/rewind 与 /fork（会话回退与分叉）。"""

from __future__ import annotations

from xenon.repl.command_groups.rewind import _cmd_fork
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL


def _make_repl(tmp_path, monkeypatch) -> REPL:
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    return REPL(registry=registry, streaming=False)


def _seed(repl: REPL) -> None:
    repl.ctx_mgr.add_user_message("任务一")
    repl.ctx_mgr.add_assistant_message("一完成")
    repl.ctx_mgr.add_user_message("任务二")
    repl.ctx_mgr.add_assistant_message("二完成")
    repl._record_event("turn/user", text="任务一", intent="debug", level=1)
    repl._record_event("tool/result", tool="write_file", success=True, paths=["a.py"])
    repl._record_event("turn/user", text="任务二", intent="debug", level=1)
    repl._record_event("tool/result", tool="write_file", success=True, paths=["b.py"])


def test_rewind_truncates_context_and_lists_artifacts(monkeypatch, tmp_path):
    repl = _make_repl(tmp_path, monkeypatch)
    _seed(repl)

    ok, artifacts = repl._rewind_to_turn(1)

    assert ok is True
    assert artifacts == ["b.py"]
    roles = [t.role for t in repl.ctx_mgr.history]
    assert roles.count("user") == 1
    assert repl._pending_action is None
    assert any(
        e["type"] == "rewind" for e in repl._session_events.read()
    )


def test_rewind_invalid_turn_fails(monkeypatch, tmp_path):
    repl = _make_repl(tmp_path, monkeypatch)
    _seed(repl)
    ok, artifacts = repl._rewind_to_turn(9)
    assert ok is False
    assert artifacts == []


def test_fork_creates_new_log_with_prefix(monkeypatch, tmp_path):
    repl = _make_repl(tmp_path, monkeypatch)
    _seed(repl)
    old_id = repl._session_events.session_id

    output = _cmd_fork(args="1", session_state={"_repl": repl})

    assert "已分叉" in output
    assert repl._session_events.session_id != old_id
    events = repl._session_events.read()
    assert any(e["type"] == "fork" and e["parent"] == old_id for e in events)
    roles = [t.role for t in repl.ctx_mgr.history]
    assert roles.count("user") == 1
