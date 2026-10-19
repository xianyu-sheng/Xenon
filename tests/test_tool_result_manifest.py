"""Save/resume manifest for structured tool-result references."""

from __future__ import annotations

from xenon.repl.context_manager import ContextManager


def test_manifest_round_trips_without_raw_content() -> None:
    cm = ContextManager()
    cm.record_tool_result(
        result_id="tr_a",
        tool="command",
        success=False,
        summary="2 failed",
        raw_sha256="abc123",
        raw_bytes=999,
        status="failed",
    )
    exported = cm.export_tool_manifest()
    assert exported[0]["result_id"] == "tr_a"
    assert exported[0]["raw_sha256"] == "abc123"

    restored = ContextManager()
    restored.restore_tool_manifest(exported)
    assert restored.export_tool_manifest() == exported


def test_manifest_drops_invalid_or_path_like_ids() -> None:
    cm = ContextManager()
    cm.restore_tool_manifest(
        [
            {"result_id": "../escape"},
            {"result_id": "not-a-result"},
            {"result_id": "tr_ok", "tool": "read_file"},
        ]
    )
    assert [item["result_id"] for item in cm.export_tool_manifest()] == ["tr_ok"]
