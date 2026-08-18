"""EvidenceGate 会话级确定性验证门测试（Step 1 骨架）。

验证：
1. 四个具体 Gate 的确定性校验行为（零 LLM）；
2. 校验与补救分离：Gate 只判定，不触发 LLM 补救；
3. PlanExecuteEngine 挂载默认管线后行为不变（向后兼容）；
4. run_gates / gate_failed / register_gate 的管线语义。
"""

from __future__ import annotations

import types

from xenon.engine.context import AgentContext
from xenon.engine.evidence_gate import (
    EvidenceCaptureGate,
    FileClaimGate,
    PlanCompletenessGate,
    TaskCompletionGate,
    plan_has_write_step,
    task_requires_write,
    verify_file_claims,
)
from xenon.engine.plan_execute_engine import PlanExecuteEngine


def _make_call(tool: str, params: dict, success: bool):
    import types as _t

    ns = _t.SimpleNamespace(
        tool_name=tool,
        params=params,
        success=success,
        error=None,
        result_summary="ok",
        attempts=1,
        elapsed_seconds=0.1,
    )
    return ns


def _make_tracker(calls):
    return types.SimpleNamespace(calls=calls)


class TestPlanCompletenessGate:
    def test_rejects_plan_without_write(self) -> None:
        gate = PlanCompletenessGate()
        verdict = gate.check(
            None,
            user_input="Fix the bug in src/main.py",
            plan={"steps": [
                {"id": 1, "task": "读", "tool": "read_file"},
                {"id": 2, "task": "分析", "tool": None},
            ]},
        )
        assert verdict.passed is False
        assert verdict.phase == "plan"

    def test_passes_plan_with_write(self) -> None:
        gate = PlanCompletenessGate()
        verdict = gate.check(
            None,
            user_input="Fix the bug in src/main.py",
            plan={"steps": [
                {"id": 1, "task": "读", "tool": "read_file"},
                {"id": 2, "task": "改", "tool": "edit_file"},
            ]},
        )
        assert verdict.passed is True

    def test_passes_read_only_task(self) -> None:
        gate = PlanCompletenessGate()
        verdict = gate.check(
            None,
            user_input="What does this function do?",
            plan={"steps": [{"id": 1, "task": "读", "tool": "read_file"}]},
        )
        assert verdict.passed is True

    def test_empty_plan_passes_to_caller(self) -> None:
        gate = PlanCompletenessGate()
        verdict = gate.check(
            None, user_input="Fix bug", plan={"steps": []},
        )
        assert verdict.passed is True  # 空计划由调用方另行处理


class TestTaskCompletionGate:
    def test_rejects_no_write_for_write_task(self) -> None:
        gate = TaskCompletionGate()
        tracker = _make_tracker([
            _make_call("read_file", {"file_path": "a.py"}, True),
        ])
        verdict = gate.check(
            None,
            user_input="Fix the bug in src/main.py",
            results=[{"step_id": 1}],
            tracker=tracker,
            max_steps=10,
        )
        assert verdict.passed is False

    def test_passes_when_write_succeeded(self) -> None:
        gate = TaskCompletionGate()
        tracker = _make_tracker([
            _make_call("edit_file", {"file_path": "a.py"}, True),
        ])
        verdict = gate.check(
            None,
            user_input="Fix the bug in src/main.py",
            results=[{"step_id": 1}],
            tracker=tracker,
            max_steps=10,
        )
        assert verdict.passed is True

    def test_passes_at_max_steps(self) -> None:
        gate = TaskCompletionGate()
        verdict = gate.check(
            None,
            user_input="Fix the bug",
            results=[{"step_id": i} for i in range(10)],
            tracker=_make_tracker([]),
            max_steps=10,
        )
        # v0.8.3: 恰达 max_steps 不再拦截（侦察型计划吃满预算后补救曾被挡，
        # 0 patch 实测）。仅远超上限（+8）时兜底，防未来循环调用失控。
        assert verdict.passed is False

    def test_passes_far_beyond_max_steps(self) -> None:
        """远超 max_steps（+8）时兜底拦截，防无限循环。"""
        gate = TaskCompletionGate()
        verdict = gate.check(
            None,
            user_input="Fix the bug",
            results=[{"step_id": i} for i in range(19)],
            tracker=_make_tracker([]),
            max_steps=10,
        )
        assert verdict.passed is True


class TestFileClaimGate:
    def test_passes_clean_output(self) -> None:
        gate = FileClaimGate()
        verdict = gate.check(None, output="已完成修复。", tracker=_make_tracker([]))
        assert verdict.passed is True

    def test_rejects_unverified_claim(self) -> None:
        gate = FileClaimGate()
        verdict = gate.check(
            None,
            output="我创建了 src/new_module.py，实现了功能。",
            tracker=_make_tracker([_make_call("read_file", {"file_path": "a.py"}, True)]),
        )
        assert verdict.passed is False
        assert verdict.payload is not None
        assert "new_module" in verdict.payload

    def test_passes_verified_claim(self) -> None:
        gate = FileClaimGate()
        tracker = _make_tracker([
            _make_call("write_file", {"file_path": "src/new_module.py"}, True),
        ])
        verdict = gate.check(
            None,
            output="我创建了 src/new_module.py，实现了功能。",
            tracker=tracker,
        )
        assert verdict.passed is True


class TestEvidenceCaptureGate:
    def test_captures_evidence(self) -> None:
        gate = EvidenceCaptureGate()
        tracker = _make_tracker([
            _make_call("edit_file", {"file_path": "a.py"}, True),
        ])
        verdict = gate.check(None, tracker=tracker)
        assert verdict.passed is True
        assert verdict.payload is not None
        assert verdict.payload.mutation_count == 1


class TestPipelineOnPlanExecuteEngine:
    def test_default_gates_mounted(self) -> None:
        eng = PlanExecuteEngine(["mock/model"], max_steps=8)
        phases = {g.phase for g in eng._gates}
        assert {"plan", "completion", "output"} <= phases

    def test_run_gates_filters_by_phase(self) -> None:
        eng = PlanExecuteEngine(["mock/model"], max_steps=8)
        verdicts = eng.run_gates(
            "plan",
            user_input="Fix the bug in src/main.py",
            plan={"steps": [{"id": 1, "task": "读", "tool": "read_file"}]},
        )
        assert len(verdicts) == 1
        assert verdicts[0].passed is False

    def test_gate_failed_returns_first_rejection(self) -> None:
        eng = PlanExecuteEngine(["mock/model"], max_steps=8)
        verdict = eng.gate_failed(
            "plan",
            user_input="Fix the bug",
            plan={"steps": [{"id": 1, "task": "读", "tool": "read_file"}]},
        )
        assert verdict is not None
        assert verdict.passed is False

    def test_gate_failed_none_when_all_pass(self) -> None:
        eng = PlanExecuteEngine(["mock/model"], max_steps=8)
        verdict = eng.gate_failed(
            "plan",
            user_input="Fix the bug",
            plan={"steps": [
                {"id": 1, "task": "读", "tool": "read_file"},
                {"id": 2, "task": "改", "tool": "edit_file"},
            ]},
        )
        assert verdict is None

    def test_register_gate_appends(self) -> None:
        eng = PlanExecuteEngine(["mock/model"], max_steps=8)
        before = len(eng._gates)
        eng.register_gate(PlanCompletenessGate())
        assert len(eng._gates) == before + 1


class TestPureHelpers:
    def test_plan_has_write_step(self) -> None:
        assert plan_has_write_step([{"tool": "read_file"}]) is False
        assert plan_has_write_step([{"tool": "write_file"}]) is True
        assert plan_has_write_step([{"tool": "command"}]) is False

    def test_task_requires_write(self) -> None:
        assert task_requires_write("Fix the bug in src/main.py") is True
        assert task_requires_write("What does this code do?") is False

    def test_verify_file_claims(self) -> None:
        passed, unverified = verify_file_claims("我创建了 a.py", None)
        assert passed is False
        assert "a.py" in unverified
        passed2, _ = verify_file_claims("分析完成", None)
        assert passed2 is True


class TestBackwardCompatibility:
    def test_engine_private_methods_delegate(self) -> None:
        """引擎私有方法保留原语义（Gate 单一真相源）。"""
        eng = PlanExecuteEngine(["mock/model"], max_steps=8)
        assert eng._plan_has_write_step([{"tool": "edit_file"}]) is True
        assert eng._plan_has_write_step([{"tool": "read_file"}]) is False
        assert eng._task_requires_write("Fix the bug in main.py") is True
        tracker = _make_tracker([_make_call("write_file", {"file_path": "a.py"}, True)])
        assert eng._has_successful_write(tracker) is True
        # 类属性向后兼容
        assert "edit_file" in eng._WRITE_TOOL_NAMES


class TestFinalizeEvidenceLayeredDelivery:
    """finalize_evidence 交付动作分层（SWE-bench 实测误杀回归）。

    背景：修复已通过写工具真正落盘（patch 已产出），但 LLM 总结里声称的
    辅助文件（复现脚本等）未经工具验证。此前 finalize_evidence 无条件
    raise，把已完成的成果整体丢弃（sphinx-7738、plan-reflection 6 例、
    react-reflection 3 例）。分层：已落盘 → 降级记录不 raise；从未落盘
    → 维持 fail-closed（防「贴 diff 不落盘」）。
    """

    @staticmethod
    def _make_engine():
        from xenon.engine.react_engine import ReActEngine

        engine = ReActEngine(
            model_priority=["deepseek/deepseek-v4-flash"],
            max_iterations=2,
            native_fc=False,
        )
        engine._begin_run()
        return engine

    @staticmethod
    def _bind(engine, ctx):
        engine._bind_evidence_ledger(ctx)
        return engine.evidence_ledger

    def test_landed_claim_does_not_raise(self) -> None:
        """已落盘 + 未验证声称 → 不 raise，成果保留（ledger 记 degraded）。"""
        engine = self._make_engine()
        ctx = AgentContext()
        ledger = self._bind(engine, ctx)
        tracker = _make_tracker([
            _make_call("edit_file", {"file_path": "src/real_fix.py"}, True),
        ])
        output = "已修改 src/real_fix.py 完成修复；并创建了 tmp/repro.py 复现脚本。"
        # 不应 raise
        engine.finalize_evidence(context=ctx, output=output, tracker=tracker)
        events = [e for e in ledger.events if getattr(e.kind, "value", str(e.kind)) == "gate_verdict"]
        degraded = [e for e in events if e.payload.get("degraded")]
        assert degraded, "已落盘场景必须记录降级警告"

    def test_no_write_still_raises(self) -> None:
        """从未落盘 + 声称创建 → 维持 fail-closed raise（贴 diff 不落盘）。"""
        engine = self._make_engine()
        ctx = AgentContext()
        self._bind(engine, ctx)
        tracker = _make_tracker([
            _make_call("read_file", {"file_path": "src/real_fix.py"}, True),
        ])
        output = "我创建了 src/real_fix.py，修改完成。"
        try:
            engine.finalize_evidence(context=ctx, output=output, tracker=tracker)
        except RuntimeError as exc:
            assert "delivery evidence gate failed" in str(exc)
        else:
            raise AssertionError("未落盘 + 声称创建必须 fail-closed raise")

    def test_clean_output_passes(self) -> None:
        engine = self._make_engine()
        ctx = AgentContext()
        self._bind(engine, ctx)
        tracker = _make_tracker([
            _make_call("edit_file", {"file_path": "src/real_fix.py"}, True),
        ])
        engine.finalize_evidence(context=ctx, output="修复完成。", tracker=tracker)
