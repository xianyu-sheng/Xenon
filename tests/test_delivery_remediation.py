"""v0.8.2 交付闸门补救循环回归测试。

背景：SWE-bench 最大失分点「贴 diff 不落盘」——LLM 声称修改/创建了
文件，但工具执行记录里没有对应写操作证据。FileClaimGate 此前在
finalize_evidence 直接 raise，拦截了但没给补救机会（拦截 ≠ 修复）。
v0.8.2 起：ReAct 增加第三纠偏循环（交付闸门预检 → 注入补救提示 →
再迭代 → 再验证），PlanExecute 增加落盘补救步骤。
"""

from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.engine.react_engine import ReActEngine


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


class TestReActDeliveryRemediation:
    def test_gate_failure_injects_remediation_then_retries(self, monkeypatch, tmp_path):
        """FileClaimGate 拦截 → 注入补救提示 → 再迭代 → 第二次成功交付。

        模拟：LLM 声称创建了 tmp/x.py 但只写过 a.py（闸门拦截），
        补救后 LLM 真正写 x.py 再交付（通过）。工具走真实 ToolExecutor
        （tracker 记录在 ToolExecutor 内部，mock _execute_tool 会绕过）。
        """
        engine = ReActEngine(
            model_priority=["deepseek/deepseek-v4-flash"],
            max_iterations=8,
            native_fc=False,
        )
        target = tmp_path / "x.py"
        calls = {"n": 0}
        remediated = {"seen": False}

        def fake_call(phase, messages, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                # 先写 a.py（有工具执行，避免触发 no_tool_streak）
                return (
                    '{"thought": "先写 a", "action": "write_file", '
                    '"action_input": {"file_path": "'
                    + str(tmp_path / "a.py")
                    + '", "content": "a = 1"}}'
                )
            if calls["n"] == 2:
                # 声称创建了 x.py，但从未写过它 → 交付闸门应拦截
                return (
                    '{"thought": "完成了", "final_answer": "已创建 '
                    + str(target)
                    + '"}'
                )
            # 第三轮起：补救提示应已注入
            joined = str(messages)
            if "不要只输出 diff" in joined:
                remediated["seen"] = True
            # 补救后真正写 x.py
            return (
                '{"thought": "落盘", "action": "write_file", '
                '"action_input": {"file_path": "'
                + str(target)
                + '", "content": "x = 1"}}'
            )

        monkeypatch.setattr(engine, "_call_llm_for_phase", fake_call)

        engine.run(
            f"创建 {target}",
            context=AgentContext(),
        )
        assert remediated["seen"], "补救提示必须注入补救轮"
        assert "x = 1" in open(target).read()

    def test_retries_exhausted_still_finalizes(self, monkeypatch, tmp_path):
        """补救次数用尽后照常 finalize（不无限循环，fail-closed 语义保留）。"""
        engine = ReActEngine(
            model_priority=["deepseek/deepseek-v4-flash"],
            max_iterations=6,
            native_fc=False,
        )
        target = tmp_path / "x.py"
        calls = {"n": 0}

        def fake_call(phase, messages, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return (
                    '{"thought": "先写 a", "action": "write_file", '
                    '"action_input": {"file_path": "'
                    + str(tmp_path / "a.py")
                    + '", "content": "a = 1"}}'
                )
            # 之后一直声称完成但从不写 x.py（闸门永远拦截）
            return '{"thought": "t", "final_answer": "完成了"}'

        monkeypatch.setattr(engine, "_call_llm_for_phase", fake_call)
        monkeypatch.setattr(
            engine,
            "delivery_gate_verdict",
            lambda **k: type("V", (), {"reason": "声称创建但无证据"})(),
        )

        # 不应无限循环：补救 2 次后接受并 finalize（最终仍 raise 由
        # finalize_evidence 的 fail-closed 语义保证，这里只验证不挂死）
        result = engine.run(f"创建 {target}", context=AgentContext())
        assert "完成" in result
        assert calls["n"] <= 8, f"不应无限循环，实际调用 {calls['n']} 次"


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
