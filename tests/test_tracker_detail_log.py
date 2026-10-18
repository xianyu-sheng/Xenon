"""E-1：写类工具的 payload 用长度展示，避免总结误判“没有传 content”。"""

from __future__ import annotations

import json

from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.tool_tracker import ToolExecutionTracker


def test_write_file_content_is_shown_as_length_not_dumped() -> None:
    tracker = ToolExecutionTracker()
    content = "print('hello')\n" * 10
    tracker.record(
        "write_file",
        {"file_path": "hello.py", "content": content},
        True,
        "写入成功",
    )

    log = tracker.detail_log()

    assert "file_path" in log and "hello.py" in log
    assert f"content=<{len(content)} chars>" in log
    # 不把整段代码塞回上下文（总结只需要知道参数存在）。
    assert "print('hello')" not in log


def test_edit_and_batch_payloads_are_summarized() -> None:
    tracker = ToolExecutionTracker()
    tracker.record(
        "edit_file",
        {"file_path": "a.py", "old_text": "old", "new_text": "new-value"},
        True,
    )
    tracker.record(
        "batch_write",
        {
            "files": [
                {"path": "a.py", "content": "a"},
                {"path": "b.py", "content": "b"},
            ]
        },
        True,
    )
    tracker.record(
        "batch_edit",
        {"edits": [{"file_path": "a.py", "old_text": "a", "new_text": "b"}]},
        True,
    )

    log = tracker.detail_log()

    assert "old_text=<3 chars>" in log
    assert "new_text=<9 chars>" in log
    assert "files=<2 items>" in log
    assert "edits=<1 items>" in log


def test_empty_content_is_still_visible() -> None:
    tracker = ToolExecutionTracker()
    tracker.record("write_file", {"file_path": "empty.txt", "content": ""}, True)
    assert "content=<0 chars>" in tracker.detail_log()


def test_summary_prompt_explains_truncated_params(monkeypatch) -> None:
    engine = PlanExecuteEngine(["fake/model"])
    captured: dict = {}

    def fake_summary(phase, messages, **kwargs):
        captured["messages"] = messages
        return "ok"

    monkeypatch.setattr(engine, "_call_llm_for_phase", fake_summary)
    tracker = ToolExecutionTracker()
    tracker.record(
        "write_file",
        {"file_path": "x.py", "content": "x" * 20},
        True,
        "写入成功",
    )
    engine._summarize("task", "analysis", [], tracker)

    text = json.dumps(captured["messages"], ensure_ascii=False)
    assert "<N chars>" in text
    assert "不要仅因未展示原文就断言参数缺失" in text
