"""Deterministic context summary and archive references (no LLM)."""

from __future__ import annotations

from xenon.repl.context_manager import ContextManager
from xenon.session.archive import build_turn_archives, render_archive_block


def test_structured_summary_keeps_goals_tools_failures_and_result_ids() -> None:
    cm = ContextManager()
    cm.add_user_message("先分析项目结构")
    cm.add_tool_trace(
        "read_file",
        {"file_path": "main.py"},
        True,
        result="def main():\n    return 1",
        result_id="tr_ok",
        raw_sha256="hash-ok",
    )
    cm.add_tool_trace(
        "command",
        {"action": "pytest"},
        False,
        result="FAILED tests/test_x.py\n2 failed",
        error="exit 1",
        result_id="tr_fail",
        raw_sha256="hash-fail",
    )
    cm.add_user_message("继续实现当前任务")

    summary = cm.structured_summary(keep_rounds=1)

    assert "用户目标" in summary
    assert "先分析项目结构" in summary
    assert "result_id=tr_ok" in summary
    assert "result_id=tr_fail" in summary
    assert "失败" in summary
    assert "0-LLM" in summary


def test_structured_summary_does_not_call_llm(monkeypatch) -> None:
    cm = ContextManager()
    cm.add_user_message("old goal")
    cm.add_user_message("new goal")
    cm.update_working_memory("session_created_files", ["out.py"])

    import xenon.utils.llm_client as llm

    def _boom(*args, **kwargs):
        raise AssertionError("structured summary must not call LLM")

    monkeypatch.setattr(llm, "chat_completion", _boom)
    summary = cm.structured_summary(keep_rounds=1)
    assert "out.py" in summary


def test_archive_keeps_result_ids_in_turn_records() -> None:
    events = [
        {"type": "turn/user", "text": "do work", "intent": "debug"},
        {
            "type": "tool/result",
            "tool": "command",
            "success": False,
            "paths": [],
            "error": "failed",
            "result_id": "tr_archive",
            "raw_sha256": "hash",
        },
        {"type": "turn/assistant", "preview": "done"},
    ]
    records = build_turn_archives(events, keep_last=0)

    assert records[0]["tools"][0]["result_id"] == "tr_archive"
    assert records[0]["tools"][0]["raw_sha256"] == "hash"
    rendered = render_archive_block(records)
    assert "tr_archive" in rendered
