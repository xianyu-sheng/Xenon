"""P2-E2 验收：PlanDAG 依赖图 + 波次并行 + DAG→串行回退 + 双模型。

覆盖（见审核文档 §Q4 / §8.7 / §8.1.1 / §8.1.6 / §8.27.1）：

- PlanDAG 单元：拓扑波次、循环/自环检测、未知依赖丢弃、重复 id 拒绝、
  str/int id 统一、标量依赖归一、波内保序、has_edges。
- parse_plan：depends_on 保留 + 标量→列表 + 默认 []。
- PlanExecuteEngine 集成：默认串行不变、depends_on 触发 DAG、并发波次
  真并发（时序）、隔离 tracker 合并、失败级联跳过、循环/重复 id 回退串行、
  双模型路由（规划 vs 执行/总结）、单步波走串行、并发单步异常转失败。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import pytest

from omniagent.engine.context import AgentContext
from omniagent.engine.plan_dag import PlanDAG, PlanDAGCycleError
from omniagent.engine.plan_execute_engine import PlanExecuteEngine
from omniagent.engine.tool_tracker import ToolExecutionTracker
from omniagent.utils.response_adapter import parse_plan


# ── PlanDAG 单元 ────────────────────────────────────────────
def _steps(*specs):
    """specs: (id, [deps]) 元组序列 → steps 列表。"""
    return [{"id": i, "task": f"t{i}", "tool": None, "params": {}, "depends_on": list(d)}
            for i, d in specs]


class TestPlanDAG:
    def test_no_deps_single_wave(self):
        dag = PlanDAG(_steps((1, []), (2, []), (3, [])))
        assert dag.waves() == [[1, 2, 3]]
        assert dag.has_edges is False

    def test_linear_chain_three_waves(self):
        dag = PlanDAG(_steps((1, []), (2, [1]), (3, [2])))
        assert dag.waves() == [[1], [2], [3]]
        assert dag.has_edges is True

    def test_diamond(self):
        # 1; 2,3 依赖 1; 4 依赖 2,3
        dag = PlanDAG(_steps((1, []), (2, [1]), (3, [1]), (4, [2, 3])))
        assert dag.waves() == [[1], [2, 3], [4]]

    def test_str_and_int_id_unified(self):
        steps = [
            {"id": 1, "task": "a", "tool": None, "params": {}, "depends_on": []},
            {"id": 2, "task": "b", "tool": None, "params": {}, "depends_on": ["1"]},
        ]
        dag = PlanDAG(steps)
        assert dag.waves() == [[1], [2]]
        assert dag.dependency_map() == {1: [], 2: [1]}

    def test_cycle_detected(self):
        dag = PlanDAG(_steps((1, [2]), (2, [1])))
        with pytest.raises(PlanDAGCycleError):
            dag.waves()

    def test_self_loop_detected(self):
        dag = PlanDAG(_steps((1, [1]), (2, [])))
        with pytest.raises(PlanDAGCycleError):
            dag.waves()

    def test_unknown_dep_ignored(self):
        dag = PlanDAG(_steps((1, [99]), (2, [])))
        # 未知依赖 99 被丢弃 → 1 无依赖，与 2 同波
        assert dag.waves() == [[1, 2]]
        assert dag.dependency_map() == {1: [], 2: []}

    def test_duplicate_id_raises(self):
        steps = [
            {"id": 1, "task": "a", "tool": None, "params": {}, "depends_on": []},
            {"id": 1, "task": "b", "tool": None, "params": {}, "depends_on": []},
        ]
        with pytest.raises(ValueError):
            PlanDAG(steps)

    def test_scalar_dep_coerced(self):
        steps = [
            {"id": 1, "task": "a", "tool": None, "params": {}, "depends_on": []},
            {"id": 2, "task": "b", "tool": None, "params": {}, "depends_on": 1},  # 标量
        ]
        dag = PlanDAG(steps)
        assert dag.dependency_map() == {1: [], 2: [1]}
        assert dag.waves() == [[1], [2]]

    def test_wave_order_preserved(self):
        # 同波内保持原始顺序
        dag = PlanDAG(_steps((3, []), (1, []), (2, [])))
        assert dag.waves() == [[3, 1, 2]]

    def test_step_lookup(self):
        dag = PlanDAG(_steps((1, []), (2, [1])))
        assert dag.step(2)["task"] == "t2"
        assert dag.step_ids == [1, 2]


# ── parse_plan depends_on ───────────────────────────────────
class TestParsePlanDependsOn:
    def test_preserves_depends_on_list(self):
        raw = json.dumps({"analysis": "a", "steps": [
            {"id": 1, "task": "a", "tool": None, "params": {}, "depends_on": []},
            {"id": 2, "task": "b", "tool": "write_file", "params": {}, "depends_on": [1]},
        ]})
        steps = parse_plan(raw)["steps"]
        assert steps[0]["depends_on"] == []
        assert steps[1]["depends_on"] == [1]

    def test_scalar_dep_coerced_to_list(self):
        raw = json.dumps({"analysis": "a", "steps": [
            {"id": 1, "task": "a", "tool": None, "params": {}},
            {"id": 2, "task": "b", "tool": None, "params": {}, "depends_on": 1},
        ]})
        steps = parse_plan(raw)["steps"]
        assert steps[0]["depends_on"] == []
        assert steps[1]["depends_on"] == [1]

    def test_default_empty_when_absent(self):
        raw = json.dumps({"analysis": "a", "steps": [
            {"id": 1, "task": "a", "tool": None, "params": {}},
        ]})
        steps = parse_plan(raw)["steps"]
        assert steps[0]["depends_on"] == []

    def test_dep_aliases(self):
        raw = json.dumps({"analysis": "a", "steps": [
            {"id": 1, "task": "a", "tool": None, "params": {}},
            {"id": 2, "task": "b", "tool": None, "params": {}, "after": [1]},
            {"id": 3, "task": "c", "tool": None, "params": {}, "requires": [1, 2]},
        ]})
        steps = parse_plan(raw)["steps"]
        assert steps[1]["depends_on"] == [1]
        assert steps[2]["depends_on"] == [1, 2]


# ── PlanExecuteEngine 集成 ──────────────────────────────────
class _PlanLLM:
    """记录调用 + 按消息内容分支返回的 _call_llm 替身。

    - 系统提示含"任务规划专家" → 返回 plan JSON
    - 系统提示含"请根据以下执行结果" → 返回 summary
    - 否则（执行步骤）→ 按 step_task 匹配 step_results/fail_tasks/raise_tasks
    """

    def __init__(self, plan, step_results=None, summary="SUMMARY",
                 fail_tasks=None, raise_tasks=None, execute_sleep=0.0):
        self.plan = plan
        self.step_results = step_results or {}
        self.summary = summary
        self.fail_tasks = set(fail_tasks or [])
        self.raise_tasks = set(raise_tasks or [])
        self.execute_sleep = execute_sleep
        self.calls = []  # [(kind, model_priority)]
        self.exec_order = []  # 执行步骤的 task 顺序

    def __call__(self, messages, max_tokens=None, *, model_priority=None):
        sys = messages[0]["content"] if messages else ""
        if "任务规划专家" in sys:
            self.calls.append(("plan", model_priority))
            return json.dumps(self.plan, ensure_ascii=False)
        if "请根据以下执行结果" in sys:
            self.calls.append(("summarize", model_priority))
            return self.summary
        # 执行步骤
        self.calls.append(("execute", model_priority))
        user = messages[-1]["content"] if messages else ""
        m = re.search(r"当前步骤: (.+)", user)
        task = m.group(1).strip() if m else ""
        self.exec_order.append(task)
        if self.execute_sleep:
            time.sleep(self.execute_sleep)
        if task in self.raise_tasks:
            raise RuntimeError(f"boom on {task}")
        if task in self.fail_tasks:
            return f"执行失败: {task}"
        for k, v in self.step_results.items():
            if k in task:
                return v
        return f"完成: {task}"


def _engine(plan, *, enable_parallel=False, executor_model_priority=None,
            model_priority=None, **llm_kw):
    eng = PlanExecuteEngine(
        model_priority or ["m1"],
        executor_model_priority=executor_model_priority,
        enable_parallel=enable_parallel,
        max_parallel_workers=4,
    )
    fake = _PlanLLM(plan, **llm_kw)
    eng._call_llm = fake
    return eng, fake


def _boom(*a, **k):
    raise AssertionError("该方法不应被调用")


class TestSerialDefault:
    def test_no_deps_uses_serial_path(self):
        """无 depends_on + 默认 enable_parallel=False → 走 _run_serial，行为不变。"""
        plan = {"analysis": "a", "steps": [
            {"id": 1, "task": "t1", "tool": None, "params": {}, "depends_on": []},
            {"id": 2, "task": "t2", "tool": None, "params": {}, "depends_on": []},
            {"id": 3, "task": "t3", "tool": None, "params": {}, "depends_on": []},
        ]}
        eng, fake = _engine(plan)
        # 若误走 DAG 路径，此处抛错
        eng._run_dag = _boom

        out = eng.run("做", AgentContext())
        assert out == "SUMMARY"
        # 三步全部执行，按序
        exec_calls = [c for c in fake.calls if c[0] == "execute"]
        assert len(exec_calls) == 3

    def test_serial_results_in_order(self):
        plan = {"analysis": "a", "steps": [
            {"id": 1, "task": "alpha", "tool": None, "params": {}, "depends_on": []},
            {"id": 2, "task": "beta", "tool": None, "params": {}, "depends_on": []},
        ]}
        eng, _ = _engine(plan, step_results={"alpha": "A", "beta": "B"})
        ctx = AgentContext()
        eng.run("做", ctx)
        assert ctx.get("step_1_result") == "A"
        assert ctx.get("step_2_result") == "B"
        assert eng._call_llm.exec_order == ["alpha", "beta"]


class TestDAGEngaged:
    def test_depends_on_engages_dag_not_serial(self):
        plan = {"analysis": "a", "steps": [
            {"id": 1, "task": "t1", "tool": None, "params": {}, "depends_on": []},
            {"id": 2, "task": "t2", "tool": None, "params": {}, "depends_on": [1]},
        ]}
        eng, _ = _engine(plan)  # enable_parallel=False，但 depends_on 存在 → DAG
        eng._run_serial = pytest.fail.__call__  # type: ignore[assignment]
        out = eng.run("做", AgentContext())
        assert out == "SUMMARY"

    def test_dag_respects_wave_order(self):
        """DAG 串行波次：step2 依赖 step1 → step1 必先执行。"""
        plan = {"analysis": "a", "steps": [
            {"id": 1, "task": "first", "tool": None, "params": {}, "depends_on": []},
            {"id": 2, "task": "second", "tool": None, "params": {}, "depends_on": [1]},
        ]}
        eng, fake = _engine(plan)
        eng.run("做", AgentContext())
        # 依赖在前：first 必须先于 second 执行
        assert fake.exec_order == ["first", "second"]


class TestParallelWaves:
    def test_parallel_runs_concurrently(self):
        """钻石计划 + 并发：wave1 两步并发，墙钟 ≈ 2×sleep 而非 3×。"""
        plan = {"analysis": "a", "steps": [
            {"id": 1, "task": "t1", "tool": None, "params": {}, "depends_on": []},
            {"id": 2, "task": "t2", "tool": None, "params": {}, "depends_on": [1]},
            {"id": 3, "task": "t3", "tool": None, "params": {}, "depends_on": [1]},
        ]}

        eng_p, _ = _engine(plan, enable_parallel=True, execute_sleep=0.4)
        t0 = time.monotonic()
        eng_p.run("做", AgentContext())
        parallel_elapsed = time.monotonic() - t0

        eng_s, _ = _engine(plan, enable_parallel=False, execute_sleep=0.4)
        t0 = time.monotonic()
        eng_s.run("做", AgentContext())
        serial_elapsed = time.monotonic() - t0

        # 并发：wave0(0.4) + wave1 并发(0.4) ≈ 0.8s；串行 3×0.4 ≈ 1.2s
        assert parallel_elapsed < 1.0, f"并发未生效: {parallel_elapsed:.2f}s"
        assert serial_elapsed > 1.1, f"串行基线异常: {serial_elapsed:.2f}s"
        assert parallel_elapsed < serial_elapsed

    def test_parallel_isolated_trackers_merged(self):
        """并发波次中每个工具步骤持有独立 tracker，波次结束合并入主 tracker。"""
        plan = {"analysis": "a", "steps": [
            {"id": 1, "task": "t1", "tool": None, "params": {}, "depends_on": []},
            {"id": 2, "task": "t2", "tool": "write_file",
             "params": {"file_path": "a.py", "content": "x"}, "depends_on": [1]},
            {"id": 3, "task": "t3", "tool": "write_file",
             "params": {"file_path": "b.py", "content": "y"}, "depends_on": [1]},
        ]}
        eng, _ = _engine(plan, enable_parallel=True)

        seen_trackers = []

        def fake_tool(tool, params, ctx, tracker):
            tracker.record(tool, params, True, "ok")
            seen_trackers.append(tracker)
            return f"工具 {tool} 完成"

        eng._execute_step_with_tool = fake_tool
        eng.run("做", AgentContext())

        # 两个工具步骤各拿到独立 tracker（无竞争）
        assert len(seen_trackers) == 2
        assert seen_trackers[0] is not seen_trackers[1]
        # 各 tracker 仅含自身调用，params 无交叉污染（顺序无关）
        paths = {t.calls[0].params["file_path"] for t in seen_trackers}
        assert paths == {"a.py", "b.py"}
        assert all(len(t.calls) == 1 for t in seen_trackers)

    def test_parallel_single_step_wave_serial(self):
        """enable_parallel=True 但每波仅 1 步 → 走 _exec_wave_serial，不抛错。"""
        plan = {"analysis": "a", "steps": [
            {"id": 1, "task": "t1", "tool": None, "params": {}, "depends_on": []},
            {"id": 2, "task": "t2", "tool": None, "params": {}, "depends_on": [1]},
        ]}
        eng, fake = _engine(plan, enable_parallel=True)
        out = eng.run("做", AgentContext())
        assert out == "SUMMARY"
        assert sum(1 for c in fake.calls if c[0] == "execute") == 2

    def test_parallel_step_exception_becomes_failure(self):
        """并发波次中单步抛异常 → 转为"执行异常"结果，不连坐整波。"""
        plan = {"analysis": "a", "steps": [
            {"id": 1, "task": "t1", "tool": None, "params": {}, "depends_on": []},
            {"id": 2, "task": "boom", "tool": None, "params": {}, "depends_on": [1]},
            {"id": 3, "task": "t3", "tool": None, "params": {}, "depends_on": [1]},
        ]}
        eng, _ = _engine(plan, enable_parallel=True, raise_tasks={"boom"})
        ctx = AgentContext()
        eng.run("做", ctx)
        # boom 步骤结果为执行异常
        assert "执行异常" in ctx.get("step_2_result")
        # t3 仍正常完成
        assert "完成" in ctx.get("step_3_result", "")


class TestSkipCascade:
    def test_failed_step_dependent_skipped(self):
        """step2 失败 → 依赖它的 step3 被跳过（修复 §8.27.1）。"""
        plan = {"analysis": "a", "steps": [
            {"id": 1, "task": "t1", "tool": None, "params": {}, "depends_on": []},
            {"id": 2, "task": "t2", "tool": None, "params": {}, "depends_on": [1]},
            {"id": 3, "task": "t3", "tool": None, "params": {}, "depends_on": [2]},
        ]}
        eng, _ = _engine(plan, fail_tasks={"t2"})  # enable_parallel=False，DAG 串行波次
        ctx = AgentContext()
        eng.run("做", ctx)
        assert "执行失败" in ctx.get("step_2_result", "")
        assert "⏭️" in ctx.get("step_3_result", "")  # 级联跳过

    def test_skip_cascade_chains(self):
        """step2 失败 → step3（依赖2）跳过 → step4（依赖3）也跳过。"""
        plan = {"analysis": "a", "steps": [
            {"id": 1, "task": "t1", "tool": None, "params": {}, "depends_on": []},
            {"id": 2, "task": "t2", "tool": None, "params": {}, "depends_on": [1]},
            {"id": 3, "task": "t3", "tool": None, "params": {}, "depends_on": [2]},
            {"id": 4, "task": "t4", "tool": None, "params": {}, "depends_on": [3]},
        ]}
        eng, _ = _engine(plan, fail_tasks={"t2"})
        ctx = AgentContext()
        eng.run("做", ctx)
        assert "⏭️" in ctx.get("step_3_result", "")
        assert "⏭️" in ctx.get("step_4_result", "")

    def test_independent_step_still_runs_after_failure(self):
        """step2 失败 → 不依赖它的 step3 仍执行（不全局中止）。"""
        plan = {"analysis": "a", "steps": [
            {"id": 1, "task": "t1", "tool": None, "params": {}, "depends_on": []},
            {"id": 2, "task": "t2", "tool": None, "params": {}, "depends_on": [1]},
            {"id": 3, "task": "t3", "tool": None, "params": {}, "depends_on": []},  # 独立
        ]}
        eng, _ = _engine(plan, fail_tasks={"t2"})
        ctx = AgentContext()
        eng.run("做", ctx)
        assert "执行失败" in ctx.get("step_2_result", "")
        assert "完成" in ctx.get("step_3_result", "")  # 独立步照常执行


class TestFallback:
    def test_cycle_falls_back_to_serial(self):
        """循环依赖 → PlanDAGCycleError → 回退串行，on_warning 触发。"""
        plan = {"analysis": "a", "steps": [
            {"id": 1, "task": "t1", "tool": None, "params": {}, "depends_on": [2]},
            {"id": 2, "task": "t2", "tool": None, "params": {}, "depends_on": [1]},
        ]}
        eng, fake = _engine(plan)
        warnings = []
        eng.callback.on_warning = lambda msg: warnings.append(msg)
        out = eng.run("做", AgentContext())
        assert out == "SUMMARY"
        assert any("串行" in w for w in warnings)
        # 串行回退后两步都执行
        assert sum(1 for c in fake.calls if c[0] == "execute") == 2

    def test_duplicate_id_falls_back_to_serial(self):
        # 含 depends_on → 触发 DAG；重复 id → ValueError → 回退串行
        plan = {"analysis": "a", "steps": [
            {"id": 1, "task": "t1", "tool": None, "params": {}, "depends_on": []},
            {"id": 2, "task": "t2", "tool": None, "params": {}, "depends_on": [1]},
            {"id": 2, "task": "t3", "tool": None, "params": {}, "depends_on": [2]},
        ]}
        eng, _ = _engine(plan)
        warnings = []
        eng.callback.on_warning = lambda msg: warnings.append(msg)
        out = eng.run("做", AgentContext())
        assert out == "SUMMARY"
        assert any("串行" in w for w in warnings)


class TestDualModel:
    def test_executor_model_priority_routed(self):
        """规划用 model_priority（_call_llm 不传 model_priority→None）；
        执行/总结用 executor_model_priority。"""
        plan = {"analysis": "a", "steps": [
            {"id": 1, "task": "t1", "tool": None, "params": {}, "depends_on": []},
            {"id": 2, "task": "t2", "tool": None, "params": {}, "depends_on": [1]},
        ]}
        eng, fake = _engine(
            plan,
            model_priority=["plan/m"],
            executor_model_priority=["exec/m"],
        )
        eng.run("做", AgentContext())

        plan_calls = [c for c in fake.calls if c[0] == "plan"]
        exec_calls = [c for c in fake.calls if c[0] == "execute"]
        sum_calls = [c for c in fake.calls if c[0] == "summarize"]
        assert plan_calls and plan_calls[0][1] is None  # 规划未显式传 model_priority
        assert exec_calls and all(c[1] == ["exec/m"] for c in exec_calls)
        assert sum_calls and all(c[1] == ["exec/m"] for c in sum_calls)

    def test_executor_defaults_to_planner_list(self):
        """未显式指定 executor_model_priority → 回退到规划模型列表。"""
        plan = {"analysis": "a", "steps": [
            {"id": 1, "task": "t1", "tool": None, "params": {}, "depends_on": []},
        ]}
        eng, fake = _engine(plan, model_priority=["only/m"])  # 无 executor_model_priority
        assert eng.executor_model_priority == ["only/m"]
        eng.run("做", AgentContext())
        exec_calls = [c for c in fake.calls if c[0] == "execute"]
        assert exec_calls and all(c[1] == ["only/m"] for c in exec_calls)
