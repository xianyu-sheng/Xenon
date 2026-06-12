from __future__ import annotations

from pathlib import Path

from omniagent.engine.callbacks import SilentCallback
from omniagent.engine.run_recorder import (
    RecordingCallback,
    RunRecorder,
    list_runs,
    load_run_events,
    summarize_run,
)


def test_run_recorder_writes_jsonl_events(tmp_path: Path):
    recorder = RunRecorder(
        goal="创建 hello.py",
        mode="react",
        model_ids=["test/model"],
        root=tmp_path,
        run_id="run-test",
        session_id="sess-test",
    )

    recorder.start()
    recorder.emit("tool.call_started", tool_name="read_file", params={"file_path": "README.md"})
    recorder.finish(status="success", result="done")

    events = load_run_events("run-test", root=tmp_path)

    assert [event["type"] for event in events] == [
        "run.started",
        "tool.call_started",
        "run.finished",
    ]
    assert events[0]["goal"] == "创建 hello.py"
    assert events[0]["mode"] == "react"
    assert events[-1]["status"] == "success"
    assert events[0]["session_id"] == "sess-test"


def test_recording_callback_forwards_and_records_tool_events(tmp_path: Path):
    recorder = RunRecorder(
        goal="task",
        mode="react",
        model_ids=["test/model"],
        root=tmp_path,
        run_id="run-callback",
    )
    recorder.start()
    delegate = SilentCallback()
    callback = RecordingCallback(delegate, recorder)

    callback.on_think("需要读取文件")
    callback.on_act("read_file", {"file_path": "README.md"})
    callback.on_observe("content")
    callback.on_finish("done")
    recorder.finish(status="success", result="done")

    assert delegate.events[0] == ("think", "需要读取文件")
    assert delegate.events[1] == ("act", ("read_file", {"file_path": "README.md"}))

    events = load_run_events("run-callback", root=tmp_path)
    event_types = [event["type"] for event in events]
    assert "agent.thought" in event_types
    assert "tool.call_started" in event_types
    assert "tool.call_finished" in event_types
    assert "agent.final_answer" in event_types


def test_list_runs_returns_recent_summaries(tmp_path: Path):
    first = RunRecorder(goal="first", mode="direct", model_ids=[], root=tmp_path, run_id="run-first")
    first.start()
    first.finish(status="success")

    second = RunRecorder(goal="second", mode="react", model_ids=[], root=tmp_path, run_id="run-second")
    second.start()
    second.finish(status="error", reason="boom")

    runs = list_runs(root=tmp_path)

    assert {run.run_id for run in runs} == {"run-first", "run-second"}
    summary = summarize_run("run-second", root=tmp_path)
    assert summary is not None
    assert summary.status == "error"
    assert summary.mode == "react"
    assert summary.session_id == ""
