"""P3-Q9 combined_engines 错误传播 + 上下文隔离测试。"""

from __future__ import annotations

from xenon.engine.combined_engines import (
    PlanReactEngine,
    PlanReflectionEngine,
    ReactReflectionEngine,
    _isolated_ctx,
)
from xenon.engine.context import AgentContext
from xenon.engine.tool_tracker import ToolExecutionTracker


# --------------------------- _isolated_ctx ---------------------------


def test_isolated_ctx_has_clean_store():
    ctx = AgentContext()
    ctx.set("step_1_result", "reactor wrote this")
    ctx.set("step_1_status", "ok")
    fresh = _isolated_ctx(ctx)
    # 新 store 不含 reactor 写入的中间状态
    assert fresh.get("step_1_result") is None
    assert fresh.get("step_1_status") is None


def test_isolated_ctx_preserves_conversation_messages():
    ctx = AgentContext()
    ctx.set_conversation_messages([{"role": "user", "content": "hi"}])
    fresh = _isolated_ctx(ctx)
    assert fresh.get_conversation_messages() == [{"role": "user", "content": "hi"}]


def test_isolated_ctx_independent_mutation():
    ctx = AgentContext()
    fresh = _isolated_ctx(ctx)
    fresh.set("new_key", "val")
    # 对原 ctx 无影响
    assert ctx.get("new_key") is None


# --------------------------- PlanReactEngine 错误传播 ---------------------------


def _plan_react_with_steps(steps_behavior):
    """构造 PlanReactEngine，planner._plan 返回固定步骤，reactor.run 按 steps_behavior。"""
    eng = PlanReactEngine(["m1"], max_steps=10, react_iterations=1)
    eng.planner._plan = lambda user_input, ctx: {
        "steps": steps_behavior,
        "analysis": "分析",
    }

    def make_run(behavior):
        def fake_run(react_input, context=None, ctx_mgr=None):
            for step_id, action in behavior:
                if f"当前步骤 ({step_id}/" in react_input:
                    if action == "fail":
                        raise RuntimeError(f"步骤{step_id}炸了")
                    return f"步骤{step_id}成功结果"
            return "ok"

        return fake_run

    eng.reactor.run = make_run([(s["id"], s["action"]) for s in steps_behavior])
    eng._summarize = lambda *a, **k: "summary"  # 跳过 LLM 汇总
    return eng


def test_plan_react_marks_failed_step_status():
    steps = [
        {"id": 1, "task": "t1", "action": "ok"},
        {"id": 2, "task": "t2", "action": "fail"},
        {"id": 3, "task": "t3", "action": "ok"},
    ]
    eng = _plan_react_with_steps(steps)
    # 捕获 results 需要访问内部——改用 _summarize 接收的 results 验证
    captured = []
    eng._summarize = lambda user_input, results, analysis="": (
        captured.append(results) or "summary"
    )
    eng.run("任务")
    results = captured[0]
    statuses = {r["step_id"]: r["status"] for r in results}
    assert statuses[1] == "ok"
    assert statuses[2] == "failed"
    assert statuses[3] == "ok"


def test_plan_react_failed_error_not_in_prev_context():
    """失败步骤的错误串不进入后续步骤的 prev_context（不当"已发现信息"）。"""
    steps = [
        {"id": 1, "task": "t1", "action": "fail"},
        {"id": 2, "task": "t2", "action": "ok"},
    ]
    eng = _plan_react_with_steps(steps)
    reactor_inputs = []
    orig_run = eng.reactor.run

    def spy_run(react_input, context=None, ctx_mgr=None):
        reactor_inputs.append(react_input)
        return orig_run(react_input, context, ctx_mgr)

    eng.reactor.run = spy_run
    eng.run("任务")
    # 步骤2的输入不应含步骤1的失败错误串
    step2_input = reactor_inputs[-1]
    assert "步骤1炸了" not in step2_input
    assert "已发现的信息" not in step2_input  # 无成功步骤 → 无 prev_context


def test_plan_react_failed_step_marked_in_summary():
    steps = [
        {"id": 1, "task": "t1", "action": "ok"},
        {"id": 2, "task": "t2", "action": "fail"},
    ]
    eng = _plan_react_with_steps(steps)
    captured = []
    eng._summarize = lambda user_input, results, analysis="": (
        captured.append(results) or "summary"
    )
    eng.run("任务")
    results = captured[0]
    failed = [r for r in results if r["status"] == "failed"]
    assert len(failed) == 1
    assert "步骤执行失败" in failed[0]["result"]


def test_plan_react_ctx_stores_status():
    steps = [{"id": 1, "task": "t1", "action": "fail"}]
    eng = _plan_react_with_steps(steps)
    ctx = AgentContext()
    eng.run("任务", context=ctx)
    assert ctx.get("step_1_status") == "failed"


# --------------------------- _summarize 区分成功/失败 ---------------------------


def test_summarize_separates_failed_steps():
    eng = PlanReactEngine(["m1"])
    results = [
        {"step_id": 1, "task": "t1", "result": "成功结果A", "status": "ok"},
        {
            "step_id": 2,
            "task": "t2",
            "result": "步骤执行失败: boom",
            "status": "failed",
        },
    ]
    summary = eng._summarize("任务", results, analysis="分析")
    # 成功结果在"执行结果"段
    assert "成功结果A" in summary
    # 失败步骤在"失败的步骤"段
    assert "失败的步骤" in summary
    assert "boom" in summary


def test_summarize_all_ok_no_failed_section():
    eng = PlanReactEngine(["m1"])
    results = [
        {"step_id": 1, "task": "t1", "result": "ok1", "status": "ok"},
    ]
    summary = eng._summarize("任务", results, analysis="分析")
    assert "失败的步骤" not in summary


def test_summarize_all_failed_shows_no_ok():
    eng = PlanReactEngine(["m1"])
    results = [
        {"step_id": 1, "task": "t1", "result": "步骤执行失败: x", "status": "failed"},
    ]
    summary = eng._summarize("任务", results, analysis="分析")
    assert "无成功完成的步骤" in summary
    assert "失败的步骤" in summary


# --------------------------- 反射引擎上下文隔离 ---------------------------


def test_react_reflection_reflector_gets_isolated_ctx():
    """reflector 收到的 context 是隔离 ctx，不含 reactor 写入的 store。"""
    eng = ReactReflectionEngine(["m1"], react_iterations=1, review_rounds=1)
    reactor_writes = {"done": False}

    def fake_reactor_run(user_input, context=None, ctx_mgr=None):
        context.set("reactor_secret", "should_not_leak")
        reactor_writes["done"] = True
        return "reactor output"

    reflector_ctxs = []

    def fake_review(user_input, output, evidence="", context=None, ctx_mgr=None):
        reflector_ctxs.append(context)
        return {"pass": True, "score": 9, "feedback": "ok", "issues": []}

    eng.reactor.run = fake_reactor_run
    eng.reflector.review_existing = fake_review
    eng.run("任务")
    assert reactor_writes["done"]
    assert len(reflector_ctxs) == 1
    # reflector 的 ctx 不含 reactor 写入的 secret
    assert reflector_ctxs[0].get("reactor_secret") is None


def test_plan_reflection_reflector_gets_isolated_ctx():
    eng = PlanReflectionEngine(["m1"], max_steps=2, review_rounds=1)

    def fake_planner_run(user_input, context=None, ctx_mgr=None):
        context.set("planner_secret", "leak?")
        return "planner output"

    reflector_ctxs = []

    def fake_review(user_input, output, evidence="", context=None, ctx_mgr=None):
        reflector_ctxs.append(context)
        return {"pass": True, "score": 9, "feedback": "ok", "issues": []}

    eng.planner.run = fake_planner_run
    eng.reflector.review_existing = fake_review
    eng.run("任务")
    assert reflector_ctxs[0].get("planner_secret") is None


def test_plan_react_skips_steps_covered_by_verified_change_and_test():
    eng = PlanReactEngine(["openai/test"], max_steps=4, react_iterations=1)
    eng.planner._plan = lambda user_input, ctx: {
        "analysis": "fix then test",
        "steps": [
            {"id": 1, "task": "分析问题"},
            {"id": 2, "task": "修复 target.py"},
            {"id": 3, "task": "运行 pytest 验证"},
            {"id": 4, "task": "总结结果"},
        ],
    }
    calls = []

    def fake_run(user_input, context=None, ctx_mgr=None):
        calls.append(user_input)
        tracker = ToolExecutionTracker()
        tracker.record("edit_file", {"file_path": "target.py"}, True, "edited")
        tracker.record("command", {"action": "pytest -q"}, True, "1 passed")
        eng.reactor._last_tracker = tracker
        return "问题已修复并通过测试"

    eng.reactor.run = fake_run
    captured = []
    eng._summarize = lambda user_input, results, analysis="": (
        captured.extend(results) or "done"
    )

    assert eng.run("修复 target.py") == "done"
    assert len(calls) == 1
    assert [item["status"] for item in captured] == [
        "ok",
        "skipped",
        "skipped",
        "skipped",
    ]
    assert len(eng._last_tracker.calls) == 2


def test_reflection_pass_preserves_executed_output_without_text_regeneration():
    eng = ReactReflectionEngine(["openai/test"], react_iterations=1)
    eng.reactor.run = lambda *args, **kwargs: "verified initial"
    eng.reactor._last_tracker = ToolExecutionTracker()
    reviews = []

    def review(*args, **kwargs):
        reviews.append(kwargs["evidence"])
        return {"pass": True, "score": 9, "feedback": "ok", "issues": []}

    eng.reflector.review_existing = review
    eng.repairer.run = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("repair must not run")
    )

    assert eng.run("回答问题") == "verified initial"
    assert len(reviews) == 1


def test_failed_review_uses_tool_capable_repair_and_aggregates_trace():
    eng = PlanReflectionEngine(["openai/test"], max_steps=1, review_rounds=1)
    initial_tracker = ToolExecutionTracker()
    initial_tracker.record("read_file", {"file_path": "x.py"}, True, "read")

    def planner_run(*args, **kwargs):
        eng.planner._last_tracker = initial_tracker
        return "only inspected"

    eng.planner.run = planner_run
    eng.reflector.review_existing = lambda *args, **kwargs: {
        "pass": False,
        "score": 3,
        "feedback": "implementation missing",
        "issues": ["file unchanged"],
    }
    eng.repairer._input_requires_tools = lambda value: True

    def repair(*args, **kwargs):
        tracker = ToolExecutionTracker()
        tracker.record("edit_file", {"file_path": "x.py"}, True, "fixed")
        tracker.record("command", {"action": "pytest -q"}, True, "passed")
        eng.repairer._last_tracker = tracker
        return "fixed and tested"

    eng.repairer.run = repair

    assert eng.run("修复 x.py") == "fixed and tested"
    assert [call.tool_name for call in eng._last_tracker.calls] == [
        "read_file",
        "edit_file",
        "command",
    ]


def test_tool_task_repair_without_real_change_keeps_initial_output():
    eng = ReactReflectionEngine(["openai/test"], react_iterations=1, review_rounds=1)
    initial_tracker = ToolExecutionTracker()

    def initial(*args, **kwargs):
        eng.reactor._last_tracker = initial_tracker
        return "initial result"

    eng.reactor.run = initial
    eng.reflector.review_existing = lambda *args, **kwargs: {
        "pass": False,
        "score": 2,
        "feedback": "not applied",
        "issues": [],
    }
    eng.repairer._input_requires_tools = lambda value: True

    def hollow_repair(*args, **kwargs):
        eng.repairer._last_tracker = ToolExecutionTracker()
        return "```diff\nnot actually applied\n```"

    eng.repairer.run = hollow_repair
    assert eng.run("修改文件") == "initial result"


def test_high_review_score_cannot_pass_write_task_without_mutation():
    eng = PlanReflectionEngine(["openai/test"], max_steps=1, review_rounds=1)

    def planner(*args, **kwargs):
        eng.planner._last_tracker = ToolExecutionTracker()
        return "I inspected the file and everything is fixed"

    eng.planner.run = planner
    eng.reflector.review_existing = lambda *args, **kwargs: {
        "pass": True,
        "score": 10,
        "feedback": "perfect",
        "issues": [],
    }
    repair_calls = []

    def repair(*args, **kwargs):
        repair_calls.append(args[0])
        tracker = ToolExecutionTracker()
        tracker.record("edit_file", {"file_path": "x.py"}, True, "fixed")
        eng.repairer._last_tracker = tracker
        return "actually fixed"

    eng.repairer.run = repair

    assert eng.run("修复 x.py") == "actually fixed"
    assert repair_calls
    assert "没有成功的状态变更工具记录" in repair_calls[0]


def test_combined_engine_reports_model_that_produced_final_output():
    eng = ReactReflectionEngine(["provider/initial"], review_rounds=1)

    def initial(*args, **kwargs):
        eng.reactor.last_model_used = "provider/initial"
        eng.reactor._last_tracker = ToolExecutionTracker()
        return "initial"

    eng.reactor.run = initial
    eng.reflector.review_existing = lambda *args, **kwargs: {
        "pass": False,
        "score": 2,
        "feedback": "revise",
        "issues": [],
    }
    eng.repairer._input_requires_tools = lambda value: False

    def repair(*args, **kwargs):
        eng.repairer.last_model_used = "provider/repair"
        eng.repairer._last_tracker = ToolExecutionTracker()
        return "repaired"

    eng.repairer.run = repair

    assert eng.run("改进这个回答") == "repaired"
    assert eng.last_model_used == "provider/repair"


def test_reflection_repair_budget_matches_parent_execution_budget():
    plan = PlanReflectionEngine(["provider/model"], max_steps=10)
    react = ReactReflectionEngine(["provider/model"], react_iterations=8)

    assert plan.repairer.max_iterations == 10
    assert react.repairer.max_iterations == 8


# ------------------- verification_loop A/B 开关传播（issue #20） -------------------


def test_plan_react_verification_flag_propagates():
    """verification_loop=False 必须传播到组合引擎自身与子引擎。

    回归 issue #20：组合引擎此前忽略 _verification_enabled，
    swebench_xenon.py 的 --no-verification-loop A/B 对照对组合引擎无效。
    """
    off = PlanReactEngine(["m1"], verification_loop=False)
    assert off._verification_enabled is False
    assert off.planner._verification_enabled is False
    assert off.reactor._verification_enabled is False


def test_plan_reflection_verification_flag_propagates():
    off = PlanReflectionEngine(["m1"], verification_loop=False)
    assert off._verification_enabled is False
    assert off.planner._verification_enabled is False
    assert off.repairer._verification_enabled is False


def test_react_reflection_verification_flag_propagates():
    off = ReactReflectionEngine(["m1"], verification_loop=False)
    assert off._verification_enabled is False
    assert off.reactor._verification_enabled is False
    assert off.repairer._verification_enabled is False


def test_combined_engines_default_verification_on():
    """默认 verification_loop=True（与 React/PlanExecute 一致）。"""
    for eng in (
        PlanReactEngine(["m1"]),
        PlanReflectionEngine(["m1"]),
        ReactReflectionEngine(["m1"]),
    ):
        assert eng._verification_enabled is True
