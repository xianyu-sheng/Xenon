"""Plan-Execute 任务完成度校验（Phase 2.5）测试。

SWE-bench 发现：plan-execute 的计划可能只有侦察步骤（read_file/search_files），
执行完「理解问题」就收工，从不实际修改文件，导致空补丁。本测试验证
``_ensure_task_completed`` 的补救执行在无写类工具时被触发。
"""

from __future__ import annotations

import pytest

from xenon.engine.context import AgentContext
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.tool_tracker import ToolExecutionTracker


class _NoopCallback:
    """最小化 EngineCallback 替身，避免导入 REPL 依赖。"""

    def on_warning(self, warning: str) -> None:
        pass

    def on_step(self, step_id: int, total: int, task: str) -> None:
        pass

    def on_step_done(self, step_id: int, success: bool, summary: str) -> None:
        pass

    def on_act(self, action: str, action_input: dict) -> None:
        pass

    def on_observe(self, observation: str) -> None:
        pass

    def on_finish(self, result: str) -> None:
        pass

    def on_review(self, score: int, passed: bool, feedback: str) -> None:
        pass

    def on_think(self, thought: str) -> None:
        pass

    def on_error(self, error: str) -> None:
        pass


@pytest.fixture
def engine() -> PlanExecuteEngine:
    """构造引擎，executor 用假写工具，避免真实 LLM 调用。"""
    callback = _NoopCallback()
    inst = PlanExecuteEngine(
        ["mock/deepseek-v4-pro"],
        max_steps=8,
        callback=callback,  # type: ignore[arg-type]
    )
    return inst


class _WriteTracker(ToolExecutionTracker):
    """tracker 带一个成功 write_file 记录。"""

    def __init__(self, *, with_write: bool = False) -> None:
        super().__init__()
        if with_write:
            self.calls.append(
                self._make_call("write_file", {"file_path": "/tmp/a.py"}, True)
            )

    @staticmethod
    def _make_call(tool: str, params: dict, success: bool):
        import types

        ns = types.SimpleNamespace(
            tool_name=tool,
            params=params,
            success=success,
            state="succeeded" if success else "failed",
            attempts=1,
            elapsed_seconds=0.1,
            result_summary="ok",
        )
        return ns


def test_task_requires_write_detects_code_fix() -> None:
    """SWE-bench 风格「fix the bug / modify」请求应判定为需要写。"""
    engine = PlanExecuteEngine(["mock/model"], max_steps=4)
    assert engine._task_requires_write(
        "You are fixing an official SWE-bench task. Implement the minimal "
        "correct fix in the working tree."
    ) is True


def test_task_requires_write_allows_read_only() -> None:
    """纯查询请求不应触发写补救。"""
    engine = PlanExecuteEngine(["mock/model"], max_steps=4)
    assert engine._task_requires_write(
        "What is the current weather in Beijing?"
    ) is False


def test_has_successful_write_true_when_write_executed() -> None:
    engine = PlanExecuteEngine(["mock/model"], max_steps=4)
    tracker = _WriteTracker(with_write=True)
    assert engine._has_successful_write(tracker) is True


def test_has_successful_write_false_when_only_reads() -> None:
    engine = PlanExecuteEngine(["mock/model"], max_steps=4)
    tracker = ToolExecutionTracker()
    # 只有只读调用
    tracker.calls.append(
        _WriteTracker._make_call("read_file", {"file_path": "/tmp/a.py"}, True)
    )
    assert engine._has_successful_write(tracker) is False


def test_ensure_skipped_when_write_done(monkeypatch) -> None:
    """已有写类工具时不追加补救步骤（零行为变化）。"""
    engine = PlanExecuteEngine(["mock/model"], max_steps=8)
    tracker = _WriteTracker(with_write=True)
    from xenon.engine.context import AgentContext
    ctx = AgentContext()

    results = [{"step_id": 1, "task": "t", "result": "r", "status": "ok"}]
    out = engine._ensure_task_completed(
        "Fix the bug in src/main.py", results, ctx, tracker, total=2
    )
    assert len(out) == 1  # 未追加
    assert out[0]["step_id"] == 1


def test_ensure_triggers_remediation_when_no_write(monkeypatch) -> None:
    """任务需要写但无写类工具 → 触发补救执行。"""
    engine = PlanExecuteEngine(["mock/model"], max_steps=8)
    tracker = ToolExecutionTracker()
    from xenon.engine.context import AgentContext
    ctx = AgentContext()

    results = [{"step_id": 1, "task": "侦察", "result": "理解了", "status": "ok"}]

    # 让 _execute_step_with_llm 返回带 write_file 声明的文本，
    # 并让跟踪器记录一个 write_file 调用（模拟补救步骤实际落盘）。
    monkeypatch.setattr(
        engine, "_execute_step_with_llm",
        lambda *a, **kw: "已通过 write_file 修改 src/main.py",
    )

    out = engine._ensure_task_completed(
        "Fix the bug in src/main.py", results, ctx, tracker, total=2
    )
    assert len(out) == 2  # 追加了补救步骤
    assert "强制补救" in out[-1]["task"]


def test_ensure_not_triggered_for_read_only_task(monkeypatch) -> None:
    """只读任务不触发补救（即使无写工具）。"""
    engine = PlanExecuteEngine(["mock/model"], max_steps=8)
    tracker = ToolExecutionTracker()
    from xenon.engine.context import AgentContext
    ctx = AgentContext()
    results = [{"step_id": 1, "task": "t", "result": "r", "status": "ok"}]

    out = engine._ensure_task_completed(
        "What is the current weather in Beijing?", results, ctx, tracker, total=2
    )
    assert len(out) == 1


# ── Phase 1.5 计划完整性校验测试 ───────────────────────────

def test_plan_has_write_step_detects_write_tools() -> None:
    engine = PlanExecuteEngine(["mock/model"], max_steps=4)
    assert engine._plan_has_write_step([
        {"id": 1, "task": "侦察", "tool": "read_file"},
        {"id": 2, "task": "修改", "tool": "write_file"},
    ]) is True


def test_plan_has_write_step_false_for_recon_only() -> None:
    engine = PlanExecuteEngine(["mock/model"], max_steps=4)
    assert engine._plan_has_write_step([
        {"id": 1, "task": "侦察", "tool": "read_file"},
        {"id": 2, "task": "分析", "tool": None},
    ]) is False


def test_ensure_plan_keeps_plan_with_write(monkeypatch) -> None:
    """计划已含写步骤时不重新规划（零行为变化）。"""
    engine = PlanExecuteEngine(["mock/model"], max_steps=8)
    ctx = AgentContext()
    plan = {"steps": [
        {"id": 1, "task": "读", "tool": "read_file"},
        {"id": 2, "task": "改", "tool": "edit_file"},
    ]}
    called = {"n": 0}
    monkeypatch.setattr(engine, "_call_llm_for_phase",
                        lambda *a, **kw: (called.__setitem__("n", called["n"] + 1) or "{}"))
    out = engine._ensure_plan_has_write_step(
        "Fix the bug in src/main.py", plan, ctx
    )
    assert len(out) == 2
    assert called["n"] == 0  # 未重新规划


def test_ensure_plan_replans_when_no_write(monkeypatch) -> None:
    """任务需写但计划无写步骤 → 重新规划。"""
    engine = PlanExecuteEngine(["mock/model"], max_steps=8)
    ctx = AgentContext()
    plan = {"steps": [
        {"id": 1, "task": "读", "tool": "read_file"},
        {"id": 2, "task": "理解", "tool": None},
    ]}
    retry_json = (
        '{"analysis":"重规划","steps":['
        '{"id":1,"task":"读","tool":"read_file"},'
        '{"id":2,"task":"改","tool":"write_file","params":{"file_path":"src/main.py"}}]}'
    )
    monkeypatch.setattr(engine, "_call_llm_for_phase",
                        lambda *a, **kw: retry_json)
    out = engine._ensure_plan_has_write_step(
        "Fix the bug in src/main.py", plan, ctx
    )
    assert len(out) == 2
    assert out[-1]["tool"] == "write_file"


def test_ensure_plan_aborts_when_replan_still_no_write(monkeypatch) -> None:
    """重新规划仍无写步骤 → 返回空（调用方终止执行）。"""
    engine = PlanExecuteEngine(["mock/model"], max_steps=8)
    ctx = AgentContext()
    plan = {"steps": [
        {"id": 1, "task": "读", "tool": "read_file"},
    ]}
    retry_json = (
        '{"analysis":"仍无写","steps":['
        '{"id":1,"task":"读","tool":"read_file"},'
        '{"id":2,"task":"分析","tool":null}]}'
    )
    monkeypatch.setattr(engine, "_call_llm_for_phase",
                        lambda *a, **kw: retry_json)
    out = engine._ensure_plan_has_write_step(
        "Fix the bug in src/main.py", plan, ctx
    )
    assert out == []


def test_remediation_forces_write_tool(monkeypatch) -> None:
    """require_write_tool=True 时，LLM 无写工具就 final_answer 会被拒绝。"""
    engine = PlanExecuteEngine(["mock/model"], max_steps=8)
    tracker = ToolExecutionTracker()
    ctx = AgentContext()

    calls = {"n": 0}

    def fake_llm(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return '{"thought":"分析","final_answer":"已理解问题"}'
        return '{"thought":"写","action":"write_file","action_input":{"file_path":"/tmp/a.py","content":"x"}}'

    monkeypatch.setattr(engine, "_call_llm_for_phase", fake_llm)

    # 记录 write_file 成功到 tracker
    def fake_tool(tool, params, context, tracker=None, **kw):
        tracker.record(
            "write_file", params, True, error=None,
            attempts=1, elapsed_seconds=0.1,
        )
        return "ok"

    monkeypatch.setattr(engine, "_execute_step_with_tool", fake_tool)

    out = engine._execute_step_with_llm(
        1, 2, "强制修改", "(无)", "Fix the bug",
        tracker=tracker, context=ctx, require_write_tool=True,
    )
    assert calls["n"] >= 2  # 第一次 final_answer 被拒绝，要求重试
    assert "write_file" in out  # 最终执行了写工具
