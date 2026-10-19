"""Phase 1: full tool results are persisted separately from evidence."""

from __future__ import annotations

import json
import time

from xenon.engine.context import AgentContext
from xenon.nodes.tool_executor import ToolExecutor
from xenon.session.tool_results import ToolResultStore


def test_store_records_raw_meta_and_redacts_params(tmp_path) -> None:
    store = ToolResultStore("session-a", root=tmp_path)
    raw = {"stdout": "full command output", "returncode": 1}
    meta = store.record(
        "exec-1",
        tool="command",
        params={"action": "pytest", "api_key": "secret-value"},
        success=False,
        observation="1 failed",
        raw=raw,
        error="exit 1",
    )

    assert meta is not None
    assert meta.result_id == "tr_exec-1"
    assert meta.raw_sha256
    assert meta.raw_bytes > 0
    assert meta.params["api_key"] == "[REDACTED]"
    assert store.read_meta(meta.result_id)["tool"] == "command"
    assert json.loads(store.read_raw(meta.result_id) or "{}") == raw


def test_store_rejects_path_like_result_ids(tmp_path) -> None:
    store = ToolResultStore("session-a", root=tmp_path)
    assert store.read_meta("../escape") is None
    assert store.read_raw("raw.json") is None


def test_store_cleanup_removes_expired_results(tmp_path) -> None:
    store = ToolResultStore("session-a", root=tmp_path, ttl_days=1)
    meta = store.record(
        "exec-old",
        tool="read_file",
        params={},
        success=True,
        observation="ok",
        raw="content",
    )
    assert meta is not None
    result_dir = store.root / meta.result_id
    old = time.time() - 3 * 86400
    os_utime = __import__("os").utime
    os_utime(result_dir, (old, old))

    assert store.cleanup() == 1
    assert not result_dir.exists()


def test_tool_executor_writes_cache_and_result_id(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sample.txt").write_text("cached full content", encoding="utf-8")
    store = ToolResultStore("session-b", root=tmp_path / "store")
    executor = ToolExecutor(result_store=store)
    ctx = AgentContext({"_session_id": "session-b"})

    result = executor.execute("read_file", {"file_path": "sample.txt"}, ctx)

    assert result.success is True
    assert result.result_id.startswith("tr_")
    meta = store.read_meta(result.result_id)
    assert meta is not None
    assert meta["tool"] == "read_file"
    raw = store.read_raw(result.result_id)
    assert raw and "cached full content" in raw
    assert result.observation  # model/evidence view unchanged


def test_store_is_opt_out(tmp_path) -> None:
    store = ToolResultStore("session-a", root=tmp_path, enabled=False)
    assert (
        store.record(
            "exec-1",
            tool="read_file",
            params={},
            success=True,
            observation="ok",
            raw="content",
        )
        is None
    )
