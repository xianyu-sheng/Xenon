"""Xenon 全面功能测试 - 自动化测试套件

这个测试文件涵盖 Xenon 的所有核心功能：
- 所有引擎模式
- 所有工具类型
- REPL 命令
- 策略指导
"""

import json
import pytest
from xenon.engine.react_engine import ReActEngine
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.combined_engines import (
    PlanReactEngine,
)
from xenon.engine.context import AgentContext
from xenon.engine.callbacks import SilentCallback


# ============================================================================
# 引擎测试
# ============================================================================


class TestEngines:
    """测试所有引擎类型"""

    def test_react_engine_basic(self, monkeypatch, tmp_path):
        """测试 ReAct 引擎基本功能"""
        callback = SilentCallback()
        engine = ReActEngine(["test/model"], callback=callback, max_iterations=3)

        def fake_llm(messages, **kwargs):
            # 模拟：读取文件 → 返回结果
            return json.dumps(
                {
                    "thought": "需要读取测试文件",
                    "action": "read_file",
                    "action_input": {"file_path": str(tmp_path / "test.txt")},
                }
            )

        # 创建测试文件
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello Xenon!")

        monkeypatch.setattr(engine, "_call_llm", fake_llm)

        # 第一次调用会读文件，第二次应该返回 final_answer
        def fake_llm_with_final(messages, **kwargs):
            if len([m for m in messages if m.get("role") == "user"]) > 1:
                return json.dumps(
                    {
                        "thought": "文件已读取",
                        "final_answer": "文件内容是: Hello Xenon!",
                    }
                )
            return fake_llm(messages, **kwargs)

        monkeypatch.setattr(engine, "_call_llm", fake_llm_with_final)

        ctx = AgentContext()
        result = engine.run("读取测试文件", ctx)

        assert "Xenon" in result or "文件" in result
        assert len([e for e, _ in callback.events if e == "act"]) >= 1

    def test_plan_execute_engine_basic(self, monkeypatch):
        """测试 Plan-Execute 引擎"""
        callback = SilentCallback()
        engine = PlanExecuteEngine(["test/model"], callback=callback, max_steps=2)

        def fake_plan(phase, messages, **kwargs):
            return json.dumps(
                {
                    "analysis": "简单的两步任务",
                    "steps": [
                        {
                            "id": 1,
                            "task": "列出当前目录",
                            "tool": "command",
                            "params": {"action": "ls -la"},
                            "depends_on": [],
                        },
                        {
                            "id": 2,
                            "task": "总结结果",
                            "tool": None,
                            "params": {},
                            "depends_on": [1],
                        },
                    ],
                }
            )

        def fake_execute(messages, **kwargs):
            return json.dumps({"thought": "执行完成", "final_answer": "目录列表已获取"})

        monkeypatch.setattr(engine, "_call_llm_for_phase", fake_plan)
        monkeypatch.setattr(engine, "_call_llm", fake_execute)

        ctx = AgentContext()
        result = engine.run("列出目录并总结", ctx)

        assert result is not None
        assert len(callback.events) > 0

    def test_plan_react_engine_basic(self, monkeypatch):
        """测试 Plan-ReAct 组合引擎"""
        callback = SilentCallback()
        engine = PlanReactEngine(
            ["test/model"], callback=callback, max_steps=1, react_iterations=2
        )

        def fake_plan(phase, messages, **kwargs):
            return json.dumps(
                {
                    "analysis": "单步任务",
                    "steps": [
                        {
                            "id": 1,
                            "task": "回答问题",
                            "tool": None,
                            "params": {},
                            "depends_on": [],
                        }
                    ],
                }
            )

        def fake_react(messages, **kwargs):
            return json.dumps({"thought": "完成", "final_answer": "任务完成"})

        monkeypatch.setattr(engine.planner, "_call_llm_for_phase", fake_plan)
        monkeypatch.setattr(engine.reactor, "_call_llm", fake_react)

        ctx = AgentContext()
        # 使用一个能触发策略识别的任务
        engine.run("修复 bug.py 并运行测试", ctx)

        # 验证策略提示被发射（组合引擎应该发射）
        tips = [v for k, v in callback.events if k == "tip"]
        # 即使是 monkeypatch 的测试，策略提示也应该在 run() 开始时被发射
        assert len(tips) >= 1, f"Expected strategy tip, got events: {callback.events}"


# ============================================================================
# 工具测试
# ============================================================================


class TestTools:
    """测试所有工具功能"""

    def test_read_file_tool(self, tmp_path, monkeypatch):
        """测试 read_file 工具"""
        test_file = tmp_path / "sample.txt"
        test_file.write_text("Sample content for testing")

        engine = ReActEngine(["test/model"], max_iterations=2)

        def fake_llm(messages, **kwargs):
            return json.dumps(
                {
                    "thought": "读取文件",
                    "action": "read_file",
                    "action_input": {"file_path": str(test_file)},
                }
            )

        def fake_llm_final(messages, **kwargs):
            if "Sample content" in str(messages):
                return json.dumps({"thought": "文件已读", "final_answer": "读取成功"})
            return fake_llm(messages, **kwargs)

        monkeypatch.setattr(engine, "_call_llm", fake_llm_final)

        ctx = AgentContext()
        result = engine.run("读取测试文件", ctx)
        assert result is not None

    def test_write_file_tool(self, tmp_path, monkeypatch):
        """测试 write_file 工具"""
        target_file = tmp_path / "output.txt"

        engine = ReActEngine(["test/model"], max_iterations=2)

        def fake_llm(messages, **kwargs):
            return json.dumps(
                {
                    "thought": "写入文件",
                    "action": "write_file",
                    "action_input": {
                        "file_path": str(target_file),
                        "content": "Test output",
                    },
                }
            )

        def fake_llm_final(messages, **kwargs):
            if target_file.exists():
                return json.dumps({"thought": "文件已写入", "final_answer": "写入成功"})
            return fake_llm(messages, **kwargs)

        monkeypatch.setattr(engine, "_call_llm", fake_llm_final)

        ctx = AgentContext()
        engine.run("创建测试文件", ctx)

        assert target_file.exists()
        assert target_file.read_text() == "Test output"

    def test_command_tool(self, monkeypatch):
        """测试 command 工具"""
        engine = ReActEngine(["test/model"], max_iterations=2)

        def fake_llm(messages, **kwargs):
            return json.dumps(
                {
                    "thought": "执行命令",
                    "action": "command",
                    "action_input": {"action": "echo 'Hello from command'"},
                }
            )

        def fake_llm_final(messages, **kwargs):
            if "Hello from command" in str(messages):
                return json.dumps({"thought": "命令已执行", "final_answer": "命令成功"})
            return fake_llm(messages, **kwargs)

        monkeypatch.setattr(engine, "_call_llm", fake_llm_final)

        ctx = AgentContext()
        result = engine.run("执行echo命令", ctx)
        assert result is not None


# ============================================================================
# 策略指导测试
# ============================================================================


class TestStrategyGuidance:
    """测试策略指导系统"""

    def test_debug_strategy_tip(self, monkeypatch):
        """测试调试任务的策略提示"""
        callback = SilentCallback()
        engine = ReActEngine(["test/model"], callback=callback, max_iterations=1)

        def fake_llm(messages, **kwargs):
            return json.dumps({"thought": "完成", "final_answer": "OK"})

        monkeypatch.setattr(engine, "_call_llm", fake_llm)

        ctx = AgentContext()
        engine.run("修复 bug.py 的 TypeError 并运行测试", ctx)

        # 验证策略提示
        tips = [v for k, v in callback.events if k == "tip"]
        assert len(tips) == 1
        assert "调试任务" in tips[0]

    def test_refactor_strategy_tip(self, monkeypatch):
        """测试重构任务的策略提示"""
        callback = SilentCallback()
        engine = ReActEngine(["test/model"], callback=callback, max_iterations=1)

        def fake_llm(messages, **kwargs):
            return json.dumps({"thought": "完成", "final_answer": "OK"})

        monkeypatch.setattr(engine, "_call_llm", fake_llm)

        ctx = AgentContext()
        engine.run("重构 UserService 类并更新所有引用", ctx)

        tips = [v for k, v in callback.events if k == "tip"]
        assert len(tips) == 1
        assert "重构任务" in tips[0]


# ============================================================================
# 运行所有测试
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
