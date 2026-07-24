"""P3-Q3 eval 框架修复单测（§8.14.1/2/3/4）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.runner import (
    RealAgent,
    load_tasks,
    run_eval,
    validate_task,
    write_report,
)


# ════════════════════════════════════════════════════════════
# §8.14.1 prompt 不暴露 expected_tools
# ════════════════════════════════════════════════════════════
class TestPromptNoLeak:
    def test_build_prompt_omits_expected_tools(self):
        task = {
            "id": "t1", "category": "file_edit",
            "prompt": "Modify src/main.py greet function",
            "expected_tools": ["read_file", "edit_file"],
            "success_criteria": "signature updated, tests pass",
        }
        prompt = RealAgent._build_prompt(task)
        assert "read_file" not in prompt
        assert "edit_file" not in prompt
        assert "Expected tools" not in prompt
        # 任务描述与类别仍在
        assert "src/main.py" in prompt
        assert "file_edit" in prompt

    def test_build_prompt_without_criteria(self):
        """success_criteria 缺失时不崩，且不暴露 expected_tools。"""
        task = {
            "id": "t1", "category": "c", "prompt": "do thing",
            "expected_tools": ["secret_tool"],
        }
        prompt = RealAgent._build_prompt(task)
        assert "secret_tool" not in prompt
        assert "do thing" in prompt


# ════════════════════════════════════════════════════════════
# §8.14.4 success_criteria 可选
# ════════════════════════════════════════════════════════════
class TestSchemaRelax:
    def test_validate_task_without_criteria_passes(self):
        task = {"id": "t", "category": "c", "prompt": "p", "expected_tools": []}
        validate_task(task)  # 不抛

    def test_validate_task_requires_id(self):
        with pytest.raises(ValueError, match="id"):
            validate_task({"category": "c", "prompt": "p", "expected_tools": []})

    def test_existing_tasks_still_load(self):
        tasks = load_tasks()
        assert len(tasks) == 20
        # 现有任务仍带 success_criteria（人类复核用）
        assert all("success_criteria" in t for t in tasks)


# ════════════════════════════════════════════════════════════
# §8.14.2 real 模式跑真实引擎，按实际执行工具评分
# ════════════════════════════════════════════════════════════
class _FakeEngine:
    """假引擎：run() 返回固定答案，_execute_tool 记录传入的 action。"""

    def __init__(self, actions_and_inputs, answer="任务完成，已写入文件"):
        self._ai = list(actions_and_inputs)
        self.answer = answer
        self.callback = None

    # 被 RealAgent 当作 ReActEngine 用
    def __call__(self, callback):
        self.callback = callback
        return self

    def _execute_tool(self, action, action_input, context, tracker=None):
        return "obs"

    def run(self, prompt, context=None, ctx_mgr=None):
        # 模拟引擎内部调用 _execute_tool（RealAgent 已包装为 _recording_execute）
        for action, action_input in self._ai:
            self._execute_tool(action, action_input, context, tracker=None)
        return self.answer


def _factory(fake_engine):
    return lambda callback: fake_engine


class TestRealAgentScoring:
    def test_score_all_expected_executed(self):
        task = {"id": "t", "category": "c", "prompt": "p",
                "expected_tools": ["read_file", "edit_file"]}
        ok, reason = RealAgent._score(task, ["read_file", "edit_file"], "done")
        assert ok is True
        assert "executed all 2" in reason

    def test_score_missing_tool_fails(self):
        task = {"id": "t", "category": "c", "prompt": "p",
                "expected_tools": ["read_file", "edit_file"]}
        ok, reason = RealAgent._score(task, ["read_file"], "done")
        assert ok is False
        assert "edit_file" in reason

    def test_score_empty_answer_fails(self):
        task = {"id": "t", "category": "c", "prompt": "p",
                "expected_tools": ["read_file"]}
        ok, reason = RealAgent._score(task, ["read_file"], "   ")
        assert ok is False
        assert "empty" in reason

    def test_run_task_records_executed_tools(self):
        """注入假引擎，验证 run_task 记录实际执行的工具并评分。"""
        fake = _FakeEngine([("read_file", {"file_path": "a.py"}),
                            ("edit_file", {"file_path": "a.py"})])
        agent = RealAgent("m1", engine_factory=_factory(fake))
        task = {"id": "t1", "category": "file_edit", "prompt": "edit a.py",
                "expected_tools": ["read_file", "edit_file"]}
        result = agent.run_task(task)
        assert result["success"] is True
        assert result["tool_calls"] == 2
        assert result["tool_failures"] == 0
        assert "read_file" in result["tools_used"]
        assert "edit_file" in result["tools_used"]

    def test_run_task_missing_tool_fails(self):
        fake = _FakeEngine([("read_file", {})])
        agent = RealAgent("m1", engine_factory=_factory(fake))
        task = {"id": "t1", "category": "file_edit", "prompt": "edit a.py",
                "expected_tools": ["read_file", "edit_file"]}
        result = agent.run_task(task)
        assert result["success"] is False
        assert result["tool_failures"] == 1
        assert "edit_file" not in result["tools_used"]

    def test_run_task_engine_exception_handled(self):
        """引擎抛异常时 run_task 不崩，记失败。"""
        class BoomEngine:
            def __call__(self, callback):
                return self
            def _execute_tool(self, *a, **k):
                return "obs"
            def run(self, p, c=None, ctx_mgr=None):
                raise RuntimeError("API 挂了")
        agent = RealAgent("m1", engine_factory=BoomEngine())
        task = {"id": "t", "category": "c", "prompt": "p", "expected_tools": ["x"]}
        result = agent.run_task(task)
        assert result["success"] is False
        assert "engine run failed" in result["notes"]

    def test_run_task_does_not_leak_expected_tools_to_engine(self):
        """传给引擎的 prompt 不含 expected_tools（防自评陷阱）。"""
        captured = {}

        class SpyEngine:
            def __call__(self, callback):
                return self
            def run(self, prompt, context=None, ctx_mgr=None):
                captured["prompt"] = prompt
                return "done"
            def _execute_tool(self, *a, **k):
                return "obs"

        agent = RealAgent("m1", engine_factory=SpyEngine())
        task = {"id": "t", "category": "c", "prompt": "do thing",
                "expected_tools": ["secret_tool_name"]}
        agent.run_task(task)
        assert "secret_tool_name" not in captured["prompt"]


# ════════════════════════════════════════════════════════════
# §8.14.3 mock 标注 smoke test
# ════════════════════════════════════════════════════════════
class TestMockSmokeTestLabel:
    def test_mock_report_has_smoke_test_disclaimer(self, tmp_path: Path):
        tasks = load_tasks()
        results = run_eval(tasks, mode="mock")
        report_path = write_report(
            results, tmp_path / "m.md", mode="mock", model="mock-agent",
            run_date="2026-07-07 00:00:00 UTC")
        report = report_path.read_text(encoding="utf-8")
        assert "smoke test" in report.lower() or "smoke" in report
        assert "NOT an agent capability" in report
        # 仍保留核心指标（向后兼容 test_evals）
        assert "Success Rate: 100.0%" in report
        assert "Tool Failures: 0" in report

    def test_real_report_has_scoring_basis(self, tmp_path: Path):
        # 用假引擎跑 real 模式（engine_factory 注入）
        fake = _FakeEngine([("read_file", {})], answer="done")
        # run_eval 不支持 engine_factory 注入，直接构造 RealAgent
        agent = RealAgent("m1", engine_factory=_factory(fake))
        tasks = load_tasks()[:1]
        results = [agent.run_task(t) for t in tasks]
        report_path = write_report(
            results, tmp_path / "r.md", mode="real", model="m1",
            run_date="2026-07-07 00:00:00 UTC")
        report = report_path.read_text(encoding="utf-8")
        assert "Scoring" in report or "实际执行" in report
