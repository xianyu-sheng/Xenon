"""Phase 2: structured model view + lazy result retrieval."""

from __future__ import annotations

import json

from xenon.engine.context import AgentContext
from xenon.nodes.tool_executor import ToolExecutor
from xenon.session.tool_results import ToolResultStore


def test_command_model_view_keeps_failure_and_result_id(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    store = ToolResultStore("view-session", root=tmp_path / "store")
    executor = ToolExecutor(result_store=store)
    ctx = AgentContext({"_session_id": "view-session"})
    raw = {
        "command": "pytest",
        "stdout": "ok\n" * 100 + "FAILED tests/test_x.py::test_y\nE assert 1 == 2\n2 failed, 1 passed\n",
        "stderr": "",
        "returncode": 1,
        "success": False,
    }

    result = executor.execute("command", {"action": "pytest"}, ctx)
    # The real command did not produce this payload; use the public helper to
    # test the projection without executing a fake shell.
    from xenon.nodes.tool_output import build_model_observation

    view = build_model_observation("command", raw, result.observation, "tr_test")
    assert view is not None
    assert "result_id: tr_test" in view
    assert "FAILED tests/test_x.py::test_y" in view
    assert "2 failed, 1 passed" in view
    assert len(view) < len(json.dumps(raw))


def test_tool_result_read_summary_range_and_search(tmp_path) -> None:
    store = ToolResultStore("read-session", root=tmp_path / "store")
    meta = store.record(
        "exec-read",
        tool="command",
        params={"action": "pytest"},
        success=False,
        observation="tests failed",
        raw={"stdout": "line1\nline2 FAILED test_x\nline3\n", "returncode": 1},
        error="exit 1",
    )
    assert meta is not None
    ctx = AgentContext({"_session_id": "read-session", "_tool_result_store": store})
    executor = ToolExecutor(result_store=store)

    summary = executor.execute(
        "tool_result_read", {"result_id": meta.result_id, "mode": "summary"}, ctx
    )
    assert summary.success
    assert "tests failed" in summary.observation
    assert "raw_sha256" in summary.observation

    ranged = executor.execute(
        "tool_result_read",
        {"result_id": meta.result_id, "mode": "range", "start_line": 2, "max_lines": 2},
        ctx,
    )
    assert ranged.success
    assert "line2 FAILED" in ranged.observation
    assert "line3" in ranged.observation

    searched = executor.execute(
        "tool_result_read",
        {"result_id": meta.result_id, "mode": "search", "query": "FAILED"},
        ctx,
    )
    assert searched.success
    assert "FAILED" in searched.observation


def test_tool_result_read_rejects_unknown_or_path_like_id(tmp_path) -> None:
    store = ToolResultStore("read-session", root=tmp_path / "store")
    ctx = AgentContext({"_session_id": "read-session", "_tool_result_store": store})
    executor = ToolExecutor(result_store=store)
    result = executor.execute(
        "tool_result_read", {"result_id": "../escape", "mode": "summary"}, ctx
    )
    assert result.success is False
    assert "缓存不存在" in (result.error or "")


def test_model_view_and_tool_result_read_are_exposed() -> None:
    from xenon.engine.react_engine import ReActEngine

    engine = ReActEngine(["fake/model"], native_fc=True)
    assert "tool_result_read" in engine.tools
    assert "tool_result_read" in engine.system_prompt
    assert "14. **结果被摘要时按需回查**" in engine.system_prompt


def test_thinking_step_carries_result_hash_for_session_events() -> None:
    from xenon.engine.callbacks import ThinkingPanel
    from xenon.nodes.tool_executor import _ToolObservation

    panel = ThinkingPanel()
    panel.add_action("read_file", {"file_path": "x.py"})
    panel.add_observation(
        _ToolObservation(
            "Observation: cached",
            success=True,
            result_id="tr_hash",
            raw_sha256="deadbeef",
        )
    )
    step = panel.steps[0]
    assert step.result_id == "tr_hash"
    assert step.raw_sha256 == "deadbeef"
