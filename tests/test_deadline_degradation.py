"""P0-3 回归：引擎超时不再整轮红错，而是返回已完成部分 + 时限说明。"""

from __future__ import annotations

from xenon.engine.callbacks import EngineCallback
from xenon.engine.combined_engines import (
    PlanReactEngine,
    _deadline_note,
    _deadline_summary,
)
from xenon.engine.execution_policy import EngineDeadlineExceeded


def test_deadline_note_mentions_budget_stop() -> None:
    note = _deadline_note()
    assert "引擎时限" in note
    assert "600" in note
    assert "继续" in note


def test_deadline_summary_lists_only_completed_work() -> None:
    results = [
        {"step_id": 1, "task": "改 schema", "result": "已完成 A", "status": "ok"},
        {"step_id": 2, "task": "写测试", "result": "超时", "status": "failed"},
    ]
    summary = _deadline_summary(results, "计划文本")
    assert "改 schema" in summary
    assert "写测试" in summary
    assert "已完成 A" in summary
    assert "失败/未完成的步骤" in summary


class _LoopDetector:
    def reset(self) -> None:
        pass


class _PlannerDeadline:
    def _plan(self, user_input, ctx):
        raise EngineDeadlineExceeded("engine deadline exceeded before planner")


def test_plan_react_planner_deadline_returns_message() -> None:
    eng = object.__new__(PlanReactEngine)
    eng.planner = _PlannerDeadline()
    eng.callback = EngineCallback()
    eng._last_tracker = None
    eng._reset_steering = lambda: None
    eng._validate_run_input = lambda text: None
    eng._finalize_plan_react = lambda ctx, output: output
    eng._loop_detector = _LoopDetector()

    output = eng.run("do something")

    assert "引擎时限" in output
    assert "规划阶段" in output


class _PlannerTwoSteps:
    def _plan(self, user_input, ctx):
        return {
            "analysis": "先做 A 再做 B",
            "steps": [
                {"id": 1, "task": "做 A"},
                {"id": 2, "task": "做 B"},
            ],
        }


class _ReactorDeadline:
    _last_tracker = None
    last_model_used = None

    def run(self, *args, **kwargs):
        raise EngineDeadlineExceeded("engine deadline exceeded before reactor")


def test_plan_react_step_deadline_returns_partial_summary() -> None:
    eng = object.__new__(PlanReactEngine)
    eng.planner = _PlannerTwoSteps()
    eng.reactor = _ReactorDeadline()
    eng.callback = EngineCallback()
    eng._last_tracker = None
    eng.max_steps = 10
    eng._reset_steering = lambda: None
    eng._drain_steering = lambda: []
    eng._validate_run_input = lambda text: None
    eng._finalize_plan_react = lambda ctx, output: output
    eng._loop_detector = _LoopDetector()

    output = eng.run("do something")

    assert "引擎时限" in output
    assert "做 A" in output
