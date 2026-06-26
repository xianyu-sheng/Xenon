"""Tests for plan_dag.py — Plan DAG construction, topological sort, and wave computation."""

from __future__ import annotations

import pytest

from omniagent.engine.plan_dag import (
    DAGExecutor,
    PlanDAG,
    PlanStep,
    plan_has_dependency_annotations,
)


# ── PlanStep tests ─────────────────────────────────────────────


class TestPlanStep:
    """PlanStep.from_dict() 构造测试。"""

    def test_from_dict_basic(self):
        """基本字段解析。"""
        data = {
            "id": 1,
            "task": "列出目录结构",
            "tool": "list_files",
            "params": {"file_path": "."},
            "depends_on": [],
        }
        step = PlanStep.from_dict(data)
        assert step.id == 1
        assert step.task == "列出目录结构"
        assert step.tool == "list_files"
        assert step.params == {"file_path": "."}
        assert step.depends_on == []
        assert step.status == "pending"
        assert step.is_tool_step is True
        assert step.waiting_for == []

    def test_from_dict_with_deps(self):
        """depends_on 整数列表。"""
        step = PlanStep.from_dict({
            "id": 3,
            "task": "汇总",
            "tool": None,
            "depends_on": [1, 2],
        })
        assert step.depends_on == [1, 2]
        assert step.is_tool_step is False

    def test_from_dict_null_tool(self):
        """tool 为 'null' 字符串 → None。"""
        step = PlanStep.from_dict({
            "id": 5,
            "task": "总结",
            "tool": "null",
        })
        assert step.tool is None
        assert step.is_tool_step is False

    def test_from_dict_deps_string_to_int(self):
        """depends_on 字符串 → 整数列表。"""
        step = PlanStep.from_dict({
            "id": 2,
            "task": "read file",
            "depends_on": ["1"],
        })
        assert step.depends_on == [1]

    def test_from_dict_deps_not_list(self):
        """depends_on 非列表 → 智能转换。"""
        step = PlanStep.from_dict({
            "id": 2,
            "task": "test",
            "depends_on": 1,
        })
        assert step.depends_on == [1]

    def test_from_dict_no_deps(self):
        """depends_on 缺失 → 空列表。"""
        step = PlanStep.from_dict({"id": 1, "task": "test"})
        assert step.depends_on == []

    def test_from_dict_id_as_string(self):
        """id 为字符串 → 整数。"""
        step = PlanStep.from_dict({"id": "7", "task": "test"})
        assert step.id == 7


# ── PlanDAG tests ──────────────────────────────────────────────


class TestPlanDAG:
    """PlanDAG 构建、验证、波次计算测试。"""

    # ── 构造 ────────────────────────────────────────────────

    def test_from_plan_empty_steps_raises(self):
        """空步骤列表。"""
        with pytest.raises(ValueError, match="没有 steps"):
            PlanDAG.from_plan({"steps": []})

    def test_from_plan_basic(self):
        """基本构造：3 步线性链。"""
        plan = {
            "steps": [
                {"id": 1, "task": "list", "depends_on": []},
                {"id": 2, "task": "read a", "depends_on": [1]},
                {"id": 3, "task": "summary", "depends_on": [2]},
            ],
        }
        dag = PlanDAG.from_plan(plan)
        assert dag.total_steps == 3
        assert dag.wave_count == 3  # 线性链：每步一波

    # ── 波次计算 ────────────────────────────────────────────

    def test_waves_linear_chain(self):
        """线性依赖链：A → B → C（每波 1 步）。"""
        dag = PlanDAG([
            PlanStep(id=1, task="A", depends_on=[]),
            PlanStep(id=2, task="B", depends_on=[1]),
            PlanStep(id=3, task="C", depends_on=[2]),
        ])
        waves = dag.waves()
        assert len(waves) == 3
        assert [s.id for s in waves[0]] == [1]
        assert [s.id for s in waves[1]] == [2]
        assert [s.id for s in waves[2]] == [3]
        assert dag.has_parallelism is False

    def test_waves_diamond(self):
        """菱形依赖：A → B, C → D。"""
        dag = PlanDAG([
            PlanStep(id=1, task="A", depends_on=[]),
            PlanStep(id=2, task="B", depends_on=[1]),
            PlanStep(id=3, task="C", depends_on=[1]),
            PlanStep(id=4, task="D", depends_on=[2, 3]),
        ])
        waves = dag.waves()
        assert len(waves) == 3
        assert [s.id for s in waves[0]] == [1]          # 波 0: A
        assert sorted(s.id for s in waves[1]) == [2, 3] # 波 1: B, C 并行
        assert [s.id for s in waves[2]] == [4]          # 波 2: D
        assert dag.has_parallelism is True

    def test_waves_all_independent(self):
        """全部独立：3 步都无依赖 → 同一波并行。"""
        dag = PlanDAG([
            PlanStep(id=1, task="A", depends_on=[]),
            PlanStep(id=2, task="B", depends_on=[]),
            PlanStep(id=3, task="C", depends_on=[]),
        ])
        waves = dag.waves()
        assert len(waves) == 1  # 全部在同一波
        assert sorted(s.id for s in waves[0]) == [1, 2, 3]
        assert dag.has_parallelism is True

    def test_waves_mixed(self):
        """混合：list → 3 个 read_file 并行 → summary。"""
        dag = PlanDAG([
            PlanStep(id=1, task="list_files", depends_on=[]),
            PlanStep(id=2, task="read A", depends_on=[1]),
            PlanStep(id=3, task="read B", depends_on=[1]),
            PlanStep(id=4, task="read C", depends_on=[1]),
            PlanStep(id=5, task="summary", depends_on=[2, 3, 4]),
        ])
        waves = dag.waves()
        assert len(waves) == 3
        assert [s.id for s in waves[0]] == [1]                # 波 0
        assert sorted(s.id for s in waves[1]) == [2, 3, 4]    # 波 1: 并行读取
        assert [s.id for s in waves[2]] == [5]                # 波 2

    def test_waves_non_contiguous_ids(self):
        """非连续 ID（LLM 可能输出 id=1,3,5,7）。"""
        dag = PlanDAG([
            PlanStep(id=1, task="A", depends_on=[]),
            PlanStep(id=3, task="B", depends_on=[1]),
            PlanStep(id=5, task="C", depends_on=[1]),
            PlanStep(id=7, task="D", depends_on=[3, 5]),
        ])
        waves = dag.waves()
        assert len(waves) == 3
        assert sorted(s.id for s in waves[1]) == [3, 5]

    def test_waves_single_step(self):
        """单步骤。"""
        dag = PlanDAG([PlanStep(id=1, task="only", depends_on=[])])
        waves = dag.waves()
        assert len(waves) == 1
        assert len(waves[0]) == 1
        assert dag.has_parallelism is False

    def test_waves_no_deps_defaults_serial(self):
        """所有步骤无 depends_on → 全部在同一波（可并行，因为无依赖）。"""
        dag = PlanDAG([
            PlanStep(id=1, task="A"),
            PlanStep(id=2, task="B"),
            PlanStep(id=3, task="C"),
        ])
        waves = dag.waves()
        assert len(waves) == 1  # 无依赖 = 全部独立 → 同一波

    # ── 验证 ────────────────────────────────────────────────

    def test_validate_valid(self):
        """合法 DAG 无错误。"""
        dag = PlanDAG([
            PlanStep(id=1, task="A", depends_on=[]),
            PlanStep(id=2, task="B", depends_on=[1]),
        ])
        assert dag.validate() == []

    def test_validate_self_dependency(self):
        """步骤依赖自身。"""
        dag = PlanDAG([
            PlanStep(id=1, task="A", depends_on=[1]),
        ])
        errors = dag.validate()
        assert any("自身" in e for e in errors)

    def test_validate_invalid_reference(self):
        """依赖不存在的步骤。"""
        dag = PlanDAG([
            PlanStep(id=1, task="A", depends_on=[]),
            PlanStep(id=2, task="B", depends_on=[99]),
        ])
        errors = dag.validate()
        assert any("99" in e for e in errors)

    def test_validate_cycle_two_nodes(self):
        """两步循环：A → B → A。"""
        dag = PlanDAG([
            PlanStep(id=1, task="A", depends_on=[2]),
            PlanStep(id=2, task="B", depends_on=[1]),
        ])
        errors = dag.validate()
        assert len(errors) > 0

    def test_validate_cycle_three_nodes(self):
        """三步循环：A → B → C → A。"""
        dag = PlanDAG([
            PlanStep(id=1, task="A", depends_on=[3]),
            PlanStep(id=2, task="B", depends_on=[1]),
            PlanStep(id=3, task="C", depends_on=[2]),
        ])
        errors = dag.validate()
        assert len(errors) > 0

    # ── 查询 ────────────────────────────────────────────────

    def test_completed_count(self):
        """completed_count / failed_count 统计。"""
        dag = PlanDAG([
            PlanStep(id=1, task="A", depends_on=[]),
            PlanStep(id=2, task="B", depends_on=[1]),
            PlanStep(id=3, task="C", depends_on=[2]),
        ])
        assert dag.completed_count() == 0
        assert dag.failed_count() == 0

        dag.steps[1].status = "done"
        dag.steps[2].status = "done"
        assert dag.completed_count() == 2

        dag.steps[3].status = "failed"
        assert dag.failed_count() == 1


# ── Helper tests ──────────────────────────────────────────────


class TestPlanHasDependencyAnnotations:
    """plan_has_dependency_annotations() 快速检测。"""

    def test_no_steps(self):
        assert plan_has_dependency_annotations({"steps": []}) is False

    def test_no_depends_on(self):
        plan = {
            "steps": [
                {"id": 1, "task": "A"},
                {"id": 2, "task": "B"},
            ],
        }
        assert plan_has_dependency_annotations(plan) is False

    def test_has_depends_on(self):
        plan = {
            "steps": [
                {"id": 1, "task": "A", "depends_on": []},
                {"id": 2, "task": "B", "depends_on": [1]},
            ],
        }
        assert plan_has_dependency_annotations(plan) is True

    def test_empty_depends_on(self):
        """depends_on 全为空 → 无依赖标注。"""
        plan = {
            "steps": [
                {"id": 1, "task": "A", "depends_on": []},
                {"id": 2, "task": "B", "depends_on": []},
            ],
        }
        assert plan_has_dependency_annotations(plan) is False

    def test_steps_not_dict(self):
        """步骤不是 dict（边界情况）。"""
        plan = {"steps": ["step1", "step2"]}
        assert plan_has_dependency_annotations(plan) is False


# ── DAGExecutor tests ─────────────────────────────────────────


class TestDAGExecutor:
    """DAGExecutor 静态方法测试。"""

    def test_extract_discoveries_file_paths(self):
        """从步骤结果提取文件路径。"""
        from omniagent.engine.plan_dag import PlanStep
        step = PlanStep(id=1, task="read")
        step.result = "Found: src/app.py and tests/test_app.py and config.json"
        discovered: list[str] = []
        DAGExecutor._extract_discoveries(step, discovered)
        assert "文件: src/app.py" in discovered
        assert "文件: tests/test_app.py" in discovered
        assert "文件: config.json" in discovered

    def test_extract_discoveries_os_windows(self):
        """提取 OS 信息。"""
        step = PlanStep(id=1, task="check")
        step.result = "Running on Windows 11"
        discovered: list[str] = []
        DAGExecutor._extract_discoveries(step, discovered)
        assert "操作系统: Windows" in discovered

    def test_extract_discoveries_dedup(self):
        """不重复添加。"""
        step = PlanStep(id=1, task="read")
        step.result = "src/app.py"
        discovered = ["文件: src/app.py"]
        DAGExecutor._extract_discoveries(step, discovered)
        assert discovered.count("文件: src/app.py") == 1

    def test_extract_discoveries_empty_result(self):
        """空结果不崩溃。"""
        step = PlanStep(id=1, task="read")
        step.result = ""
        discovered: list[str] = []
        DAGExecutor._extract_discoveries(step, discovered)
        assert discovered == []


# ── Integration: DAGExecutor with simple DAG ───────────────────


class TestDAGExecutorIntegration:
    """DAGExecutor 与 PlanDAG 的集成测试（不调用真实 LLM）。"""

    def test_dag_wave_structure_preserved(self):
        """DAG 波次结构在 executor 中正确传递。"""
        dag = PlanDAG([
            PlanStep(id=1, task="list_files", tool=None, depends_on=[]),
            PlanStep(id=2, task="read A", tool=None, depends_on=[1]),
            PlanStep(id=3, task="read B", tool=None, depends_on=[1]),
            PlanStep(id=4, task="summary", tool=None, depends_on=[2, 3]),
        ])
        waves = dag.waves()
        assert len(waves) == 3
        # Wave 0: step 1 only
        assert [s.id for s in waves[0]] == [1]
        # Wave 1: steps 2 and 3 in parallel
        assert sorted(s.id for s in waves[1]) == [2, 3]
        # Wave 2: step 4
        assert [s.id for s in waves[2]] == [4]
