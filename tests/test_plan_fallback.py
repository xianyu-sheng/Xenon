"""P0-2：规划解析失败时降级为单步 ReAct，而不是返回原始 JSON。"""

from __future__ import annotations

from types import SimpleNamespace

from xenon.engine.callbacks import EngineCallback
from xenon.engine.plan_execute_engine import PlanExecuteEngine


class _Callback(EngineCallback):
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def on_warning(self, message: str) -> None:
        self.warnings.append(message)


def test_fallback_steps_is_one_llm_step() -> None:
    steps = PlanExecuteEngine._fallback_steps("do the thing")
    assert len(steps) == 1
    assert steps[0]["task"] == "do the thing"
    assert steps[0]["tool"] is None
    assert steps[0]["depends_on"] == []


def test_run_degrades_instead_of_returning_raw_plan() -> None:
    engine = object.__new__(PlanExecuteEngine)
    callback = _Callback()
    seen: dict[str, object] = {}

    engine.callback = callback
    engine.max_steps = 10
    engine.enable_parallel = False
    engine._last_tracker = None
    engine._ctx_mgr = None
    engine._validate_run_input = lambda text: text
    engine._reset_interrupt = lambda: None
    engine._reset_steering = lambda: None
    engine._begin_run = lambda: None
    engine._bind_evidence_ledger = lambda ctx: None
    engine._loop_detector = SimpleNamespace(reset=lambda: None)
    engine._plan = lambda ui, ctx: {
        "analysis": '{"raw":"plan"}',
        "steps": [],
        "parse_failed": True,
    }
    engine._ensure_plan_has_write_step = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("fallback must skip the write-step re-plan")
    )
    engine._normalize_incomplete_edit_steps = lambda steps: steps

    def _run_serial(capped, user_input, ctx, tracker, total):
        seen["steps"] = capped
        return [
            {
                "step_id": 1,
                "task": capped[0]["task"],
                "result": "done",
                "status": "ok",
            }
        ]

    engine._run_serial = _run_serial
    engine._ensure_task_completed = lambda *a, **k: a[1]
    engine.gate_failed = lambda *a, **k: None
    engine._summarize = lambda *a, **k: "summary"
    engine.finalize_evidence = lambda **k: None

    result = engine.run("do the thing")

    assert result == "summary"
    assert '{"raw":"plan"}' not in result
    assert seen["steps"][0]["tool"] is None
    assert any("降级" in warning for warning in callback.warnings)
