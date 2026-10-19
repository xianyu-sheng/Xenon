"""步数预算：默认不限步数；XENON_MAX_ITERATIONS 可显式设置安全上限。"""

from __future__ import annotations

from xenon.engine.builtin_engines import (
    STANDARD_STEP_BUDGET,
    _make_plan_execute,
    _make_plan_react,
    _make_plan_reflection,
    _make_react,
    _make_react_reflection,
    _step_budget,
)
from xenon.engine.react_engine import ReActEngine


def test_standard_step_budget_is_unlimited():
    """0 means unlimited; LoopDetector/no-progress/deadline are the gates."""

    assert STANDARD_STEP_BUDGET == 0


def test_all_step_based_factories_default_to_unlimited(monkeypatch):
    """All step-based engines default to unlimited (0)."""

    monkeypatch.delenv("XENON_MAX_ITERATIONS", raising=False)
    kwargs = dict(
        model_priority=["m1"], model_pool=None, auto_router=None, callback=None
    )
    assert _make_react(**kwargs).max_iterations == 0
    assert _make_plan_execute(**kwargs).max_steps == 0
    plan_react = _make_plan_react(**kwargs)
    assert plan_react.max_steps == 0
    assert plan_react.reactor.max_iterations == 0
    assert _make_plan_reflection(**kwargs).max_steps == 0
    assert _make_react_reflection(**kwargs).reactor.max_iterations == 0


def test_step_budget_default_and_env(monkeypatch):
    monkeypatch.delenv("XENON_MAX_ITERATIONS", raising=False)
    assert _step_budget(STANDARD_STEP_BUDGET) == 0
    assert _step_budget(24) == 24

    monkeypatch.setenv("XENON_MAX_ITERATIONS", "120")
    assert _step_budget(40) == 120

    monkeypatch.setenv("XENON_MAX_ITERATIONS", "garbage")
    assert _step_budget(40) == 40  # 无效值回退默认

    monkeypatch.setenv("XENON_MAX_ITERATIONS", "0")
    assert _step_budget(40) == 0  # explicit unlimited


def test_factory_reads_env_budget(monkeypatch):
    monkeypatch.setenv("XENON_MAX_ITERATIONS", "88")
    engine = _make_react(model_priority=["m1"], model_pool=None)
    assert isinstance(engine, ReActEngine)
    assert engine.max_iterations == 88


def test_plan_react_uses_budget(monkeypatch):
    monkeypatch.setenv("XENON_MAX_ITERATIONS", "60")
    engine = _make_plan_react(
        model_priority=["m1"], model_pool=None, auto_router=None, callback=None
    )
    assert engine.max_steps == 60


def test_budget_exhaustion_answer_is_marked(monkeypatch):
    """预算耗尽路径必须诚实标注，不能伪装成正常完成。"""

    class _Tracker:
        calls: list = []

        def get_summary(self):
            return "x"

    class _CB:
        def __init__(self):
            self.finished: list = []

        def on_finish(self, msg):
            self.finished.append(msg)

        def on_warning(self, msg):
            pass

        def on_error(self, *a):
            pass

        def on_tool_result(self, *a, **k):
            pass

        def on_step(self, *a, **k):
            pass

    eng = ReActEngine.__new__(ReActEngine)
    eng.max_iterations = 5
    eng.callback = _CB()
    eng._mercy_compile = lambda u, t, m: "整理结果"

    out = eng._mark_budget_exhausted("整理结果")
    assert out.startswith("⚠️ 已达到步数上限（5 步）")
    assert "整理结果" in out
    assert "任务可能未完成" in out


def test_budget_manager_zero_means_unlimited_until_external_gate():
    from xenon.engine.budget import BudgetManager

    budget = BudgetManager(max_iterations=0)
    assert budget.unlimited is True
    assert budget.can_continue() is True
    budget.spend(10_000)
    assert budget.can_continue() is True
    assert budget.phase.value == "explore"
    budget.on_retry()
    assert budget.bonus > 0
