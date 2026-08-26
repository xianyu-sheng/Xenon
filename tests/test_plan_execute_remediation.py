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
from xenon.nodes.tool_executor import ToolExecuteResult


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

    def on_tip(self, tip: str) -> None:
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
    assert (
        engine._task_requires_write(
            "You are fixing an official SWE-bench task. Implement the minimal "
            "correct fix in the working tree."
        )
        is True
    )


def test_task_requires_write_allows_read_only() -> None:
    """纯查询请求不应触发写补救。"""
    engine = PlanExecuteEngine(["mock/model"], max_steps=4)
    assert (
        engine._task_requires_write("What is the current weather in Beijing?") is False
    )


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
        engine,
        "_execute_step_with_llm",
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
    assert (
        engine._plan_has_write_step(
            [
                {"id": 1, "task": "侦察", "tool": "read_file"},
                {"id": 2, "task": "修改", "tool": "write_file"},
            ]
        )
        is True
    )


def test_plan_has_write_step_false_for_recon_only() -> None:
    engine = PlanExecuteEngine(["mock/model"], max_steps=4)
    assert (
        engine._plan_has_write_step(
            [
                {"id": 1, "task": "侦察", "tool": "read_file"},
                {"id": 2, "task": "分析", "tool": None},
            ]
        )
        is False
    )


def test_ensure_plan_keeps_plan_with_write(monkeypatch) -> None:
    """计划已含写步骤时不重新规划（零行为变化）。"""
    engine = PlanExecuteEngine(["mock/model"], max_steps=8)
    ctx = AgentContext()
    plan = {
        "steps": [
            {"id": 1, "task": "读", "tool": "read_file"},
            {"id": 2, "task": "改", "tool": "edit_file"},
        ]
    }
    called = {"n": 0}
    monkeypatch.setattr(
        engine,
        "_call_llm_for_phase",
        lambda *a, **kw: called.__setitem__("n", called["n"] + 1) or "{}",
    )
    out = engine._ensure_plan_has_write_step("Fix the bug in src/main.py", plan, ctx)
    assert len(out) == 2
    assert called["n"] == 0  # 未重新规划


def test_ensure_plan_replans_when_no_write(monkeypatch) -> None:
    """任务需写但计划无写步骤 → 重新规划。"""
    engine = PlanExecuteEngine(["mock/model"], max_steps=8)
    ctx = AgentContext()
    plan = {
        "steps": [
            {"id": 1, "task": "读", "tool": "read_file"},
            {"id": 2, "task": "理解", "tool": None},
        ]
    }
    retry_json = (
        '{"analysis":"重规划","steps":['
        '{"id":1,"task":"读","tool":"read_file"},'
        '{"id":2,"task":"改","tool":"write_file","params":{"file_path":"src/main.py"}}]}'
    )
    monkeypatch.setattr(engine, "_call_llm_for_phase", lambda *a, **kw: retry_json)
    out = engine._ensure_plan_has_write_step("Fix the bug in src/main.py", plan, ctx)
    assert len(out) == 2
    assert out[-1]["tool"] == "write_file"


def test_ensure_plan_aborts_when_replan_still_no_write(monkeypatch) -> None:
    """重新规划仍无写步骤 → 降级执行原计划（Phase 2.5 补救兜底落盘）。

    v0.8.3 修复：放弃 = 必然 0 patch（SWE-bench django-16408 官方 API
    实测）；返回原侦察计划，由 _ensure_task_completed 的强制写补救兜底。
    """
    engine = PlanExecuteEngine(["mock/model"], max_steps=8)
    ctx = AgentContext()
    plan = {
        "steps": [
            {"id": 1, "task": "读", "tool": "read_file"},
        ]
    }
    retry_json = (
        '{"analysis":"仍无写","steps":['
        '{"id":1,"task":"读","tool":"read_file"},'
        '{"id":2,"task":"分析","tool":null}]}'
    )
    monkeypatch.setattr(engine, "_call_llm_for_phase", lambda *a, **kw: retry_json)
    out = engine._ensure_plan_has_write_step("Fix the bug in src/main.py", plan, ctx)
    assert out == plan["steps"]  # 降级执行原计划而非放弃


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
            "write_file",
            params,
            True,
            error=None,
            attempts=1,
            elapsed_seconds=0.1,
        )
        return "ok"

    monkeypatch.setattr(engine, "_execute_step_with_tool", fake_tool)

    out = engine._execute_step_with_llm(
        1,
        2,
        "强制修改",
        "(无)",
        "Fix the bug",
        tracker=tracker,
        context=ctx,
        require_write_tool=True,
    )
    assert calls["n"] >= 2  # 第一次 final_answer 被拒绝，要求重试
    assert "write_file" in out  # 最终执行了写工具


class TestToolStepRemediation:
    """工具步骤失败自动降级迷你 ReAct 补救（SWE-bench 实测修复）。

    背景：plan 阶段预生成 params 一次性执行，失败即跳过（plan-execute
    23 空 patch 的最大根因之一：缺 file_path/old_text、匹配失败、证据门
    拦截）。修复后：失败的工具步骤转迷你 ReAct，LLM 现场重新生成参数。
    """

    def _engine(self, attempts=1):
        return PlanExecuteEngine(
            ["provider/model"],
            callback=_NoopCallback(),
            tool_remediation_attempts=attempts,
            max_mini_react_rounds=1,
        )

    def test_tool_failure_triggers_mini_react_remediation(self, monkeypatch):
        """缺参数失败 → 补救轮 LLM 重新生成正确参数 → 步骤成功。"""
        engine = self._engine()
        calls = {"tool": 0, "llm": 0}
        tracker = ToolExecutionTracker()

        def fake_tool(tool, params, ctx, tracker=None, **kw):
            calls["tool"] += 1
            if calls["tool"] == 1:
                tracker.record(
                    tool,
                    params,
                    False,
                    "ValueError: [exec_edit_file] edit_file 需要 file_path",
                )
                return ToolExecuteResult(
                    "edit_file",
                    False,
                    "ValueError: [exec_edit_file] edit_file 需要 file_path",
                    error="ValueError: edit_file 需要 file_path",
                )
            tracker.record(tool, params, True, "编辑成功")
            return ToolExecuteResult("edit_file", True, "编辑成功")

        def fake_llm(phase, messages, **kw):
            calls["llm"] += 1
            # 补救 prompt 必须携带失败原因
            joined = str(messages)
            assert "需要 file_path" in joined
            return (
                '{"thought": "重新生成参数", "action": "edit_file", '
                '"action_input": {"file_path": "a.py", "old_text": "x", '
                '"new_text": "y"}}'
            )

        monkeypatch.setattr(engine._tool_executor, "execute", fake_tool)
        monkeypatch.setattr(engine, "_call_llm_for_phase", fake_llm)

        outcome = engine._execute_tool_step(
            1,
            3,
            "修改 a.py",
            "edit_file",
            {},
            "修复 bug",
            AgentContext(),
            tracker,
            "(无)",
        )
        assert outcome.success is True
        assert calls["tool"] == 2
        assert calls["llm"] == 1
        # 补救的工具调用必须进了 tracker（patch 才能生成）
        assert any(c.success and c.tool_name == "edit_file" for c in tracker.calls)

    def test_user_denial_never_remediated(self, monkeypatch):
        """用户拒绝/权限拒绝 → 不补救（重试会绕过用户意志）。"""
        engine = self._engine(attempts=3)
        llm_called = {"n": 0}
        tracker = ToolExecutionTracker()

        def fake_tool(tool, params, ctx, **kw):
            return ToolExecuteResult(
                "write_file",
                False,
                "⛔ 操作被拒绝: 用户拒绝",
                error="用户拒绝",
                cancelled=True,
            )

        monkeypatch.setattr(engine._tool_executor, "execute", fake_tool)
        monkeypatch.setattr(
            engine,
            "_call_llm_for_phase",
            lambda *a, **k: llm_called.__setitem__("n", llm_called["n"] + 1) or "",
        )

        outcome = engine._execute_tool_step(
            1,
            1,
            "写文件",
            "write_file",
            {},
            "写文件",
            AgentContext(),
            tracker,
            "(无)",
        )
        assert outcome.success is False
        assert llm_called["n"] == 0, "用户拒绝不得触发补救 LLM 调用"

    def test_success_no_remediation(self, monkeypatch):
        engine = self._engine()
        llm_called = {"n": 0}
        tracker = ToolExecutionTracker()

        def fake_tool(tool, params, ctx, **kw):
            return ToolExecuteResult("read_file", True, "内容")

        monkeypatch.setattr(engine._tool_executor, "execute", fake_tool)
        monkeypatch.setattr(
            engine,
            "_call_llm_for_phase",
            lambda *a, **k: llm_called.__setitem__("n", llm_called["n"] + 1) or "",
        )

        outcome = engine._execute_tool_step(
            1,
            1,
            "读文件",
            "read_file",
            {},
            "读文件",
            AgentContext(),
            tracker,
            "(无)",
        )
        assert outcome.success is True
        assert llm_called["n"] == 0

    def test_attempts_exhausted_returns_last_failure(self, monkeypatch):
        """补救次数用尽后返回最终失败，不无限循环。"""
        engine = self._engine(attempts=2)
        calls = {"tool": 0, "llm": 0}

        def fake_tool(tool, params, ctx, tracker=None, **kw):
            calls["tool"] += 1
            if tracker is not None:
                tracker.record(tool, params, False, "未找到匹配文本")
            return ToolExecuteResult(
                "edit_file",
                False,
                "未找到匹配文本",
                error="未找到匹配文本",
            )

        monkeypatch.setattr(engine._tool_executor, "execute", fake_tool)
        monkeypatch.setattr(
            engine,
            "_call_llm_for_phase",
            lambda *a, **k: (
                calls.__setitem__("llm", calls["llm"] + 1)
                or '{"thought": "t", "action": "edit_file", "action_input": {"file_path": "a.py"}}'
            ),
        )

        outcome = engine._execute_tool_step(
            1,
            1,
            "改 a.py",
            "edit_file",
            {},
            "改 a.py",
            AgentContext(),
            ToolExecutionTracker(),
            "(无)",
        )
        assert outcome.success is False
        assert calls["tool"] == 3  # 初始 1 + 补救 2
        assert calls["llm"] == 2


class TestWriteStepPreservationAndRemediationBudget:
    """SWE-bench 实测修复（django-16408 官方 API 0 patch 根因）：

    1. 计划截断不得砍掉写步骤（cap 后全是侦察 → 置换保留写步骤）；
    2. _ensure_task_completed 的补救不再被 max_steps 硬拦截
       （侦察型计划吃满预算后补救曾被挡 → 0 patch）。
    """

    def test_cap_preserves_write_step_beyond_budget(self, monkeypatch):
        engine = PlanExecuteEngine(
            ["provider/model"],
            callback=_NoopCallback(),
            max_steps=3,
        )
        monkeypatch.setattr(
            engine,
            "_plan",
            lambda *a, **k: {
                "analysis": "a",
                "steps": [
                    {"id": 1, "task": "读", "tool": "read_file", "params": {}},
                    {"id": 2, "task": "搜", "tool": "search_files", "params": {}},
                    {"id": 3, "task": "分析", "tool": None, "params": {}},
                    {
                        "id": 4,
                        "task": "编辑",
                        "tool": "edit_file",
                        "params": {
                            "file_path": "a.py",
                            "old_text": "x",
                            "new_text": "y",
                        },
                    },
                ],
            },
        )
        captured = {}

        def fake_serial(steps, *a, **k):
            captured["steps"] = steps
            return []

        monkeypatch.setattr(engine, "_run_serial", fake_serial)
        monkeypatch.setattr(
            engine,
            "_execute_step_with_llm",
            lambda *a, **k: "已写入",
        )
        monkeypatch.setattr(engine, "_summarize", lambda *a, **k: "完成")

        engine.run("修复 a.py 的 bug", AgentContext())
        # cap=3 但写步骤（edit_file）必须被置换保留进执行计划
        tools = [s.get("tool") for s in captured["steps"]]
        assert "edit_file" in tools, f"写步骤被截断丢弃: {tools}"

    def test_ensure_task_completed_remediates_at_max_steps(self, monkeypatch):
        """侦察步骤吃满 max_steps 后，补救仍必须触发（不再被硬拦截）。"""
        engine = PlanExecuteEngine(
            ["provider/model"],
            callback=_NoopCallback(),
            max_steps=2,
        )
        tracker = ToolExecutionTracker()
        # 前 2 步全是成功侦察（读），无任何写，且结果数已达 max_steps
        for i in range(2):
            tracker.record("read_file", {"file_path": f"a{i}.py"}, True, "ok")
        results = [
            {"step_id": 1, "task": "读1", "result": "ok", "status": "ok"},
            {"step_id": 2, "task": "读2", "result": "ok", "status": "ok"},
        ]
        ctx = AgentContext()

        def fake_remediation(
            step_id,
            total,
            task,
            prev,
            original,
            tracker,
            context=None,
            require_write_tool=False,
            steering=None,
        ):
            assert require_write_tool is True, "补救必须强制写工具"
            tracker.record(
                "write_file", {"file_path": "c.py", "content": "x"}, True, "ok"
            )
            return "已写入 c.py"

        monkeypatch.setattr(engine, "_execute_step_with_llm", fake_remediation)

        results = engine._ensure_task_completed(
            "修复 bug（需要写文件）",
            results,
            ctx,
            tracker,
            2,
        )
        # max_steps=2 已满，补救必须仍追加并真正写盘
        assert len(results) == 3, "max_steps 已满时补救被硬拦截（0 patch 根因）"
        assert any(c.tool_name == "write_file" and c.success for c in tracker.calls)


class TestToolRemediationRequiresWrite:
    """补救成功判定收紧：写工具步骤失败后，只补 read 不算修复。

    SWE-bench 实测（django-16408）：edit_file 缺 old_text 失败 → 补救轮
    LLM 只 read_file 就被旧判定当成已修复（任意成功调用）→ 0 patch。
    """

    def test_read_only_remediation_not_counted_as_fix(self, monkeypatch):
        engine = PlanExecuteEngine(
            ["provider/model"],
            callback=_NoopCallback(),
            tool_remediation_attempts=1,
            max_mini_react_rounds=1,
        )
        tracker = ToolExecutionTracker()
        calls = {"tool": 0}

        def fake_tool(tool, params, ctx, tracker=None, **kw):
            calls["tool"] += 1
            if calls["tool"] == 1:
                tracker.record(tool, params, False, "需要 file_path")
                return ToolExecuteResult(
                    "edit_file",
                    False,
                    "需要 file_path",
                    error="需要 file_path",
                )
            # 补救轮 LLM 只读文件（成功）——不算修复
            tracker.record(tool, params, True, "read ok")
            return ToolExecuteResult(tool, True, "read ok")

        monkeypatch.setattr(engine._tool_executor, "execute", fake_tool)
        monkeypatch.setattr(
            engine,
            "_call_llm_for_phase",
            lambda *a, **k: (
                '{"thought": "先读一下", "action": "read_file", '
                '"action_input": {"file_path": "a.py"}}'
            ),
        )

        outcome = engine._execute_tool_step(
            1,
            3,
            "编辑 a.py",
            "edit_file",
            {},
            "修复 bug",
            AgentContext(),
            tracker,
            "(无)",
        )
        # 只读补救不算成功 → 步骤仍失败（避免 0 patch 假阳性）
        assert outcome.success is False
        assert "未执行成功" in (outcome.error or "")

    def test_write_remediation_counts_as_fix(self, monkeypatch):
        engine = PlanExecuteEngine(
            ["provider/model"],
            callback=_NoopCallback(),
            tool_remediation_attempts=1,
            max_mini_react_rounds=1,
        )
        tracker = ToolExecutionTracker()
        calls = {"tool": 0}

        def fake_tool(tool, params, ctx, tracker=None, **kw):
            calls["tool"] += 1
            if calls["tool"] == 1:
                tracker.record(tool, params, False, "需要 file_path")
                return ToolExecuteResult(
                    "edit_file",
                    False,
                    "需要 file_path",
                    error="需要 file_path",
                )
            tracker.record(tool, params, True, "edit ok")
            return ToolExecuteResult("edit_file", True, "edit ok")

        monkeypatch.setattr(engine._tool_executor, "execute", fake_tool)
        monkeypatch.setattr(
            engine,
            "_call_llm_for_phase",
            lambda *a, **k: (
                '{"thought": "重新生成参数", "action": "edit_file", '
                '"action_input": {"file_path": "a.py", "old_text": "x", '
                '"new_text": "y"}}'
            ),
        )

        outcome = engine._execute_tool_step(
            1,
            3,
            "编辑 a.py",
            "edit_file",
            {},
            "修复 bug",
            AgentContext(),
            tracker,
            "(无)",
        )
        assert outcome.success is True
        assert any(c.tool_name == "edit_file" and c.success for c in tracker.calls)


class TestIncompleteEditStepNormalization:
    """edit 步骤参数不全 → 转 LLM 步骤（SWE-bench django-16408 实测）。

    plan 阶段未读文件，edit_file 缺 old_text/new_text 时确定性执行必然
    失败；转为 tool=None 后迷你 ReAct 现场先读后写。
    """

    def test_edit_missing_params_becomes_llm_step(self) -> None:
        engine = PlanExecuteEngine(["provider/model"], callback=_NoopCallback())
        steps = [
            {
                "id": 1,
                "task": "读",
                "tool": "read_file",
                "params": {"file_path": "a.py"},
            },
            {
                "id": 2,
                "task": "改",
                "tool": "edit_file",
                "params": {"file_path": "a.py"},
            },  # 缺 old_text/new_text
        ]
        out = engine._normalize_incomplete_edit_steps(steps)
        assert out[1]["tool"] is None
        assert "read_file" in out[1]["task"]  # 提示先读后写
        assert out[0]["tool"] == "read_file"  # 完整步骤不动

    def test_edit_with_full_params_kept(self) -> None:
        engine = PlanExecuteEngine(["provider/model"], callback=_NoopCallback())
        steps = [
            {
                "id": 1,
                "task": "改",
                "tool": "edit_file",
                "params": {"file_path": "a.py", "old_text": "x", "new_text": "y"},
            },
        ]
        out = engine._normalize_incomplete_edit_steps(steps)
        assert out[0]["tool"] == "edit_file"

    def test_batch_edit_incomplete_becomes_llm_step(self) -> None:
        engine = PlanExecuteEngine(["provider/model"], callback=_NoopCallback())
        steps = [
            {
                "id": 1,
                "task": "批量改",
                "tool": "batch_edit",
                "params": {"edits": [{"file_path": "a.py", "old_text": "x"}]},
            },
        ]
        out = engine._normalize_incomplete_edit_steps(steps)
        assert out[0]["tool"] is None
