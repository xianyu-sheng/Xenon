"""Regression tests for structured Plan-Execute success propagation."""

from __future__ import annotations

from xenon.engine.callbacks import EngineCallback
from xenon.engine.context import AgentContext
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.tool_tracker import ToolExecutionTracker
from xenon.nodes.tool_executor import ToolExecuteResult


class CaptureCallback(EngineCallback):
    def __init__(self):
        self.done: list[tuple[object, bool, str]] = []

    def on_step_done(self, step_id, success, summary=""):
        self.done.append((step_id, success, summary))


def _failure(tool="write_file"):
    return ToolExecuteResult(
        tool,
        False,
        "⛔ 操作被拒绝: 用户拒绝",
        error="用户拒绝",
    )


def test_serial_uses_native_success_flag_not_text_prefix(monkeypatch):
    callback = CaptureCallback()
    engine = PlanExecuteEngine(["model"], callback=callback)
    monkeypatch.setattr(engine._tool_executor, "execute", lambda *args, **kwargs: _failure())
    ctx = AgentContext()

    results = engine._run_serial(
        [{"id": 1, "task": "write", "tool": "write_file", "params": {}}],
        "task",
        ctx,
        ToolExecutionTracker(),
        1,
    )

    assert results[0]["status"] == "failed"
    assert results[0]["error"] == "用户拒绝"
    assert ctx.get("step_1_status") == "failed"
    assert callback.done[0][1] is False


def test_native_success_is_not_overridden_by_failure_like_content(monkeypatch):
    engine = PlanExecuteEngine(["model"])
    native = ToolExecuteResult(
        "read_file",
        True,
        "执行失败案例.md 的内容读取成功",
    )
    monkeypatch.setattr(engine._tool_executor, "execute", lambda *args, **kwargs: native)

    results = engine._run_serial(
        [{"id": 1, "task": "read", "tool": "read_file", "params": {}}],
        "task",
        AgentContext(),
        ToolExecutionTracker(),
        1,
    )

    assert results[0]["status"] == "ok"


def test_dag_skips_dependency_after_structured_tool_failure(monkeypatch):
    engine = PlanExecuteEngine(["model"])
    monkeypatch.setattr(engine._tool_executor, "execute", lambda *args, **kwargs: _failure())
    steps = [
        {
            "id": 1,
            "task": "write",
            "tool": "write_file",
            "params": {},
            "depends_on": [],
        },
        {
            "id": 2,
            "task": "verify",
            "tool": None,
            "params": {},
            "depends_on": [1],
        },
    ]
    ctx = AgentContext()

    results = engine._run_dag(
        steps,
        "task",
        ctx,
        ToolExecutionTracker(),
        2,
    )

    assert [result["status"] for result in results] == ["failed", "skipped"]
    assert ctx.get("step_2_status") == "skipped"


def test_previous_result_context_excludes_failures_and_skips():
    previous = PlanExecuteEngine._build_prev_results([
        {"step_id": 1, "result": "verified", "status": "ok"},
        {"step_id": 2, "result": "secret failure details", "status": "failed"},
        {"step_id": 3, "result": "skipped", "status": "skipped"},
    ])

    assert "verified" in previous
    assert "failure" not in previous
    assert "skipped" not in previous


def test_summary_prompt_contains_explicit_step_statuses(monkeypatch):
    engine = PlanExecuteEngine(["model"])
    captured = {}

    def fake_llm(messages, model_priority=None):
        captured["messages"] = messages
        return "summary"

    monkeypatch.setattr(engine, "_call_llm", fake_llm)
    result = engine._summarize(
        "task",
        "analysis",
        [
            {"step_id": 1, "task": "a", "result": "done", "status": "ok"},
            {"step_id": 2, "task": "b", "result": "denied", "status": "failed"},
        ],
    )

    assert result == "summary"
    prompt = captured["messages"][1]["content"]
    assert "[OK]" in prompt
    assert "[FAILED]" in prompt


def test_planner_retries_once_after_unparseable_response(monkeypatch):
    engine = PlanExecuteEngine(["model"])
    responses = iter([
        "<tool_calls>not a plan</tool_calls>",
        '{"analysis":"修复任务","steps":[{"id":1,"task":"读取文件",'
        '"tool":"read_file","params":{"file_path":"a.py"}}]}',
    ])
    monkeypatch.setattr(engine, "_call_llm_for_phase", lambda *a, **k: next(responses))

    result = engine._plan("修复 a.py", AgentContext())

    assert len(result["steps"]) == 1
    assert result["steps"][0]["tool"] == "read_file"
