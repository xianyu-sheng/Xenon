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
    # The hint must be the literal call, not a function-looking name: the
    # model otherwise put "tool_result_read" into the file_path slot.
    hint = view.split("完整结果可按需读取：", 1)[1]
    assert json.loads(hint)["params"]["result_id"] == "tr_test"
    assert json.loads(hint)["prefer"] == "tool_result_read"


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


def test_tool_result_read_is_registered() -> None:
    """The tool must remain dispatchable by name.

    The engine's prompt copy is gone; the registry is the surviving source of
    truth for what the runtime can call.
    """

    from xenon.nodes.tool_registry import BUILTIN_TOOL_METHODS

    assert "tool_result_read" in BUILTIN_TOOL_METHODS


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


def test_search_model_view_reports_count_and_incompleteness() -> None:
    """#432: the model has to be told how many hits there were.

    The legacy search text was up to 50 ``file:line`` rows and no total, so
    "how many matches" was unanswerable even though the tool knew the number.
    """

    from xenon.nodes.tool_output import build_model_observation

    raw = {
        "action_type": "search_files",
        "pattern": "mcpServers",
        "matches": [
            {"file": "a.json", "line": 1, "content": '"mcpServers": {}'},
            {"file": "b.json", "line": 4, "content": '"mcpServers": {}'},
        ],
        "match_count": 137,
        "returned_count": 2,
        "files_scanned": 900,
        "truncated": True,
        "scan_truncated": True,
        "next_cursor": "2",
    }

    view = build_model_observation("search", raw, "", "tr_search")

    assert view is not None
    assert "matches=137" in view
    assert "scanned=900" in view
    assert "truncated=True" in view
    assert "result_id: tr_search" in view
    assert "a.json:1" in view
    assert "扫描达到文件/时间上限" in view


def test_search_without_a_pattern_reports_a_listing() -> None:
    from xenon.nodes.tool_output import build_model_observation

    raw = {
        "action_type": "list_files",
        "files": ["a.py", "b.py"],
        "count": 2,
    }

    view = build_model_observation("search", raw, "", "tr_list")

    assert view is not None
    assert view.startswith("[list_files] count=2")


def test_search_model_view_says_when_nothing_matched() -> None:
    from xenon.nodes.tool_output import build_model_observation

    raw = {
        "action_type": "search_files",
        "pattern": "nope",
        "matches": [],
        "match_count": 0,
        "files_scanned": 7,
    }

    view = build_model_observation("search", raw, "", "tr_zero")

    assert view is not None
    assert "matches=0" in view
    assert "（无匹配条目）" in view
    # 没有 match_count 的载荷不该被读成「0 命中」——退回本页条数。
    partial = {"matches": [{"file": "a.py", "line": 1, "content": "x"}]}
    fallback = build_model_observation("search", partial, "", "tr_partial")
    assert fallback is not None
    assert "matches=1" in fallback


def test_search_model_view_states_skipped_large_files() -> None:
    from xenon.nodes.tool_output import build_model_observation

    raw = {
        "matches": [{"file": "a.py", "line": 2, "content": "hit"}],
        "match_count": 1,
        "files_scanned": 9,
        "skipped_large": 4,
    }

    view = build_model_observation("search", raw, "", "tr_skip")

    assert view is not None
    assert "跳过 4 个超过大小上限的文件" in view
