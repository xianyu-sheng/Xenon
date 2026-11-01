"""EvidenceGate 会话级确定性验证门测试（Step 1 骨架）。

验证：
1. 四个具体 Gate 的确定性校验行为（零 LLM）；
2. 校验与补救分离：Gate 只判定，不触发 LLM 补救；
3. 纯函数判定（`task_requires_write` / `verify_file_claims`）的确定性行为。

引擎私有的管线（`run_gates` / `gate_failed` / `finalize_evidence`）随引擎删除；
kernel 侧等价判断由审计 + 债务覆盖（见 `tests/kernel/test_audit.py`）。
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
            user_input="把结果写到 src/main.py",
            plan={
                "steps": [
                    {"id": 1, "task": "读", "tool": "read_file"},
                    {"id": 2, "task": "分析", "tool": None},
                ]
            },
        )
        assert verdict.passed is False
        assert verdict.phase == "plan"

    def test_passes_plan_with_write(self) -> None:
        gate = PlanCompletenessGate()
        verdict = gate.check(
            None,
            user_input="把结果写到 src/main.py",
            plan={
                "steps": [
                    {"id": 1, "task": "读", "tool": "read_file"},
                    {"id": 2, "task": "改", "tool": "edit_file"},
                ]
            },
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
            None,
            user_input="Fix bug",
            plan={"steps": []},
        )
        assert verdict.passed is True  # 空计划由调用方另行处理


class TestTaskCompletionGate:
    def test_rejects_no_write_for_write_task(self) -> None:
        gate = TaskCompletionGate()
        tracker = _make_tracker(
            [
                _make_call("read_file", {"file_path": "a.py"}, True),
            ]
        )
        verdict = gate.check(
            None,
            user_input="把结果写到 src/main.py",
            results=[{"step_id": 1}],
            tracker=tracker,
            max_steps=10,
        )
        assert verdict.passed is False

    def test_passes_when_write_succeeded(self) -> None:
        gate = TaskCompletionGate()
        tracker = _make_tracker(
            [
                _make_call("edit_file", {"file_path": "a.py"}, True),
            ]
        )
        verdict = gate.check(
            None,
            user_input="把结果写到 src/main.py",
            results=[{"step_id": 1}],
            tracker=tracker,
            max_steps=10,
        )
        assert verdict.passed is True

    def test_passes_at_max_steps(self) -> None:
        gate = TaskCompletionGate()
        verdict = gate.check(
            None,
            user_input="把结果写到 output.txt",
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
            user_input="把结果写到 output.txt",
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
            tracker=_make_tracker(
                [_make_call("read_file", {"file_path": "a.py"}, True)]
            ),
        )
        assert verdict.passed is False
        assert verdict.payload is not None
        assert "new_module" in verdict.payload

    def test_passes_verified_claim(self) -> None:
        gate = FileClaimGate()
        tracker = _make_tracker(
            [
                _make_call("write_file", {"file_path": "src/new_module.py"}, True),
            ]
        )
        verdict = gate.check(
            None,
            output="我创建了 src/new_module.py，实现了功能。",
            tracker=tracker,
        )
        assert verdict.passed is True


class TestEvidenceCaptureGate:
    def test_captures_evidence(self) -> None:
        gate = EvidenceCaptureGate()
        tracker = _make_tracker(
            [
                _make_call("edit_file", {"file_path": "a.py"}, True),
            ]
        )
        verdict = gate.check(None, tracker=tracker)
        assert verdict.passed is True
        assert verdict.payload is not None
        assert verdict.payload.mutation_count == 1


class TestPureHelpers:
    def test_plan_has_write_step(self) -> None:
        assert plan_has_write_step([{"tool": "read_file"}]) is False
        assert plan_has_write_step([{"tool": "write_file"}]) is True
        assert plan_has_write_step([{"tool": "command"}]) is False

    def test_task_requires_write(self) -> None:
        # 语义识别属于分类器：写权限由本轮契约决定；无契约时正则只认显式结构。
        from xenon.repl.execution_policy import ExecutionLevel
        from xenon.repl.turn_contract import contract_for_level

        write_ctx = AgentContext(
            {"_turn_contract": contract_for_level(ExecutionLevel.WRITE)}
        )
        read_ctx = AgentContext(
            {"_turn_contract": contract_for_level(ExecutionLevel.READ_ONLY)}
        )
        assert task_requires_write("fix the bug", ctx=write_ctx) is True
        assert task_requires_write("fix the bug", ctx=read_ctx) is False
        assert task_requires_write("把结果写到 output.txt") is True
        assert task_requires_write("What does this code do?") is False

    def test_verify_file_claims(self) -> None:
        passed, unverified = verify_file_claims("我创建了 a.py", None)
        assert passed is False
        assert "a.py" in unverified
        passed2, _ = verify_file_claims("分析完成", None)
        assert passed2 is True
