"""发布门规则回归测试。

背景：SWE-bench 最大失分点「贴 diff 不落盘」——LLM 声称修改/创建了
文件，但工具执行记录里没有对应写操作证据。此前 FileClaimGate 在引擎内
拦截（交付闸门补救循环），验证统一到回合级 TurnGate 后，该规则由
TurnGate 的文件声称审计承接；写入后验证失败（原 VerificationLoop）与
空洞回答（原 HollowDetector）同样上移。
"""

from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.engine.react_engine import ReActEngine
from xenon.turn.gate import VERDICT_FAIL, VERDICT_PASS, TurnGate


class TestDeliveryRemediationPrompt:
    def test_prompt_contains_reason_and_action(self):
        class _V:
            reason = "LLM 声称创建但未经工具验证的文件: tmp/x.py"

        eng = ReActEngine(
            model_priority=["deepseek/deepseek-v4-flash"],
            max_iterations=3,
            native_fc=False,
        )
        prompt = eng.delivery_remediation_prompt(_V())
        assert "tmp/x.py" in prompt
        assert "write_file" in prompt
        assert "不要只输出 diff" in prompt


class TestTurnGateFileClaims:
    """原引擎交付闸门语义 → 回合级 TurnGate 文件声称审计。"""

    def test_unverified_file_claim_fails_gate(self, tmp_path):
        """声称创建 x.py 但只写过 a.py → TurnGate fail。"""

        gate = TurnGate()
        target = tmp_path / "x.py"
        events = [
            {
                "tool": "write_file",
                "success": True,
                "params": {"file_path": str(tmp_path / "a.py")},
            }
        ]
        verdict = gate.evaluate(f"已创建 {target}", tool_events=events)
        assert verdict.outcome == VERDICT_FAIL
        assert any("未经工具验证" in r for r in verdict.reasons)

    def test_verified_claim_passes_gate(self, tmp_path):
        target = tmp_path / "x.py"
        gate = TurnGate()
        events = [
            {
                "tool": "write_file",
                "success": True,
                "params": {"file_path": str(target)},
            }
        ]
        verdict = gate.evaluate(f"已创建 {target}，内容已写入", tool_events=events)
        assert verdict.outcome == VERDICT_PASS

    def test_retries_do_not_loop_forever_fuse(self, tmp_path):
        """同因失败熔断：不会无限重试（fail-closed）。"""

        gate = TurnGate(max_same_cause_failures=2)
        target = tmp_path / "x.py"
        events = [
            {
                "tool": "write_file",
                "success": True,
                "params": {"file_path": str(tmp_path / "a.py")},
            }
        ]
        first = gate.evaluate(f"已创建 {target}", tool_events=events)
        second = gate.evaluate(f"已创建 {target}", tool_events=events)
        assert first.outcome == VERDICT_FAIL
        assert second.outcome == "fuse"


class TestTurnGateWriteVerify:
    """原引擎 VerificationLoop 语义 → 回合级写入后验证规则。"""

    def test_write_then_failed_test_without_disclosure_fails(self):
        gate = TurnGate()
        events = [
            {
                "tool": "write_file",
                "success": True,
                "params": {"file_path": "x.py"},
            },
            {"tool": "command", "success": False, "error": "assert 失败"},
        ]
        verdict = gate.evaluate("修复完成", tool_events=events)
        assert verdict.outcome == VERDICT_FAIL
        assert any("写入后验证" in r for r in verdict.reasons)

    def test_disclosed_failure_passes(self):
        gate = TurnGate()
        events = [
            {
                "tool": "write_file",
                "success": True,
                "params": {"file_path": "x.py"},
            },
            {"tool": "command", "success": False, "error": "assert 失败"},
        ]
        verdict = gate.evaluate(
            "写入完成，但测试仍失败：assert 失败（需要进一步排查）",
            tool_events=events,
        )
        assert verdict.outcome == VERDICT_PASS

    def test_successful_test_passes(self):
        gate = TurnGate()
        events = [
            {
                "tool": "write_file",
                "success": True,
                "params": {"file_path": "x.py"},
            },
            {"tool": "command", "success": True, "error": ""},
        ]
        verdict = gate.evaluate("已修改 x.py 并运行测试，全部通过。", tool_events=events)
        assert verdict.outcome == VERDICT_PASS


class TestTurnGateHollow:
    """原引擎空洞回答拦截 → 回合级空洞规则。"""

    def test_hollow_after_tools_fails(self):
        gate = TurnGate()
        events = [{"tool": "read_file", "success": True}]
        verdict = gate.evaluate("好的", tool_events=events)
        assert verdict.outcome == VERDICT_FAIL
        assert any("空洞" in r for r in verdict.reasons)

    def test_short_chat_without_tools_passes(self):
        gate = TurnGate()
        verdict = gate.evaluate("好的", tool_events=[])
        assert verdict.outcome == VERDICT_PASS

    def test_completion_claim_without_tools_is_hollow(self):
        """宣称完成但无任何工具与产物结构 → 空洞（即使无工具回执）。"""

        gate = TurnGate()
        verdict = gate.evaluate("已完成修复", tool_events=[])
        assert verdict.outcome == VERDICT_FAIL
        assert any("空洞" in r for r in verdict.reasons)


class TestVerificationLoop:
    """验证链闭环（v0.8.2）：测试失败反馈到修复循环。"""

    def test_failed_test_triggers_repair_round(self, monkeypatch, tmp_path):
        from xenon.engine.plan_execute_engine import PlanExecuteEngine

        engine = PlanExecuteEngine(
            model_priority=["deepseek/deepseek-v4-flash"],
            max_steps=10,
        )
        plan = (
            '{"analysis": "计划", "steps": ['
            '{"id": 1, "task": "修改代码", "tool": "write_file", '
            '"params": {"file_path": "'
            + str(tmp_path / "x.py")
            + '", "content": "def f(): return 1"}}, '
            '{"id": 2, "task": "运行测试", "tool": "command", '
            '"params": {"command": "python -m pytest '
            + str(tmp_path / "test_x.py")
            + '"}}]}'
        )
        repair_seen = {"called": False}

        def fake_call(phase, messages, **kwargs):
            if phase == "plan":
                return plan
            if phase == "execute_step":
                if "验证闭环" in str(messages):
                    repair_seen["called"] = True
                return '{"thought": "t", "final_answer": "步骤完成"}'
            return "汇总完成"

        monkeypatch.setattr(engine, "_call_llm_for_phase", fake_call)
        # 写工具真实执行；测试命令模拟失败（returncode 非零）

        def fake_cmd(tool, params, ctx, tracker, **kwargs):
            if tool == "command":
                tracker.record(
                    "command",
                    params,
                    False,
                    "assert 失败: expected 2 got 1",
                    error="FAILED test_x.py::test_f",
                )
                return "❌ 测试失败: assert 失败"
            # write_file 真实写
            from xenon.nodes.tool_executor import ToolExecutor

            return (
                ToolExecutor()
                .execute(tool, params, ctx, tracker=tracker)
                .format_observation()
            )

        monkeypatch.setattr(engine, "_execute_step_with_tool", fake_cmd)

        engine.run("修复 bug 并跑测试验证", context=AgentContext())
        assert repair_seen["called"], "测试失败必须触发验证闭环修复轮"

    def test_successful_test_skips_repair(self, monkeypatch, tmp_path):
        """测试通过时不触发修复轮（防成本膨胀）。"""
        from xenon.engine.plan_execute_engine import PlanExecuteEngine

        engine = PlanExecuteEngine(
            model_priority=["deepseek/deepseek-v4-flash"],
            max_steps=10,
        )
        plan = (
            '{"analysis": "计划", "steps": ['
            '{"id": 1, "task": "修改代码", "tool": "write_file", '
            '"params": {"file_path": "'
            + str(tmp_path / "x.py")
            + '", "content": "x=1"}}, '
            '{"id": 2, "task": "运行测试", "tool": "command", '
            '"params": {"command": "python -m pytest '
            + str(tmp_path / "test_x.py")
            + '"}}]}'
        )
        repair_seen = {"called": False}

        def fake_call(phase, messages, **kwargs):
            if phase == "plan":
                return plan
            if phase == "execute_step":
                if "验证闭环" in str(messages):
                    repair_seen["called"] = True
                return '{"thought": "t", "final_answer": "步骤完成"}'
            return "汇总完成"

        monkeypatch.setattr(engine, "_call_llm_for_phase", fake_call)

        def fake_cmd(tool, params, ctx, tracker, **kwargs):
            if tool == "command":
                tracker.record("command", params, True, "1 passed")
                return "✅ 1 passed"
            from xenon.nodes.tool_executor import ToolExecutor

            return (
                ToolExecutor()
                .execute(tool, params, ctx, tracker=tracker)
                .format_observation()
            )

        monkeypatch.setattr(engine, "_execute_step_with_tool", fake_cmd)

        engine.run("修复 bug 并跑测试验证", context=AgentContext())
        assert not repair_seen["called"], "测试通过时不应触发修复轮"
