"""v0.6.1: 子 Agent 系统 P0/P1/P2 测试。"""
import pytest
from unittest.mock import MagicMock, patch
import unittest.mock
from xenon.engine.react_engine import ReActEngine, BUILTIN_TOOLS
from xenon.engine.context import AgentContext


class TestSubAgentSystem:
    """子 Agent 系统测试套件。"""

    def _make_engine(self, **kwargs):
        """创建一个测试用 ReActEngine。"""
        mock_callback = MagicMock()
        return ReActEngine(
            ["test-model"],
            callback=mock_callback,
            **kwargs,
        )

    # ── P0: 超时控制 ──────────────────────────────────

    def test_subagent_timeout_param_default(self):
        """subagent_timeout 默认值为 None。"""
        eng = self._make_engine()
        assert eng.subagent_timeout is None

    def test_subagent_timeout_param_set(self):
        """subagent_timeout 可以显式设置。"""
        eng = self._make_engine(subagent_timeout=30)
        assert eng.subagent_timeout == 30

    def test_spawn_empty_task_rejected(self):
        """空任务被拒绝。"""
        eng = self._make_engine()
        ctx = AgentContext()
        result = eng._spawn_subagent({}, ctx, None)
        assert "非空 task" in result

    def test_spawn_depth_limit_reached(self):
        """深度超限被拒绝。"""
        eng = self._make_engine(max_subagent_depth=0)
        ctx = AgentContext()
        result = eng._spawn_subagent({"task": "test"}, ctx, None)
        assert "深度超限" in result

    def test_all_eight_engine_types_buildable(self):
        """所有 8 种引擎类型均可构建。"""
        eng = self._make_engine()
        expected = {
            "react": "ReActEngine",
            "plan_execute": "PlanExecuteEngine",
            "reflection": "ReflectionEngine",
            "novel": "NovelEngine",
            "plan_react": "PlanReactEngine",
            "plan_reflection": "PlanReflectionEngine",
            "react_reflection": "ReactReflectionEngine",
            "direct": "ReActEngine",
        }
        for etype, cls_name in expected.items():
            sub = eng._build_sub_engine(etype, f"t-{etype}")
            assert not isinstance(sub, str), f"{etype} 构建失败: {sub}"
            assert cls_name in type(sub).__name__, f"{etype} 期望 {cls_name}, 实际 {type(sub).__name__}"

    def test_invalid_engine_rejected_with_all_options(self):
        """不支持的引擎类型列举所有 8 种可用选项。"""
        eng = self._make_engine()
        result = eng._build_sub_engine("nonexistent", "id")
        assert "plan_react" in result
        assert "plan_reflection" in result
        assert "react_reflection" in result
        assert "novel" in result
        assert "direct" in result

    def test_build_sub_engine_react(self):
        """构建 ReAct 子引擎。"""
        eng = self._make_engine()
        sub = eng._build_sub_engine("react", "test-id")
        assert isinstance(sub, ReActEngine)
        assert sub._subagent_depth == 1

    def test_build_sub_engine_direct_has_no_tools(self):
        """direct 引擎不应暴露工具。"""
        eng = self._make_engine()
        sub = eng._build_sub_engine("direct", "test-id")
        assert isinstance(sub, ReActEngine)
        assert sub.tools == {}

    # ── P2: 并行子 Agent ──────────────────────────────

    def test_spawn_all_too_many_tasks(self):
        """task_list 超过 10 个被拒绝。"""
        eng = self._make_engine()
        ctx = AgentContext()
        result = eng._spawn_all_subagents(["task"] * 11, ctx, None)
        assert "最多 10 个" in result

    def test_spawn_all_invalid_task_item(self):
        """task_list 中无效元素被拒绝。"""
        eng = self._make_engine()
        ctx = AgentContext()
        result = eng._spawn_all_subagents([123], ctx, None)
        assert "格式无效" in result

    def test_spawn_all_missing_task_field(self):
        """task_list 中缺少 task 字段被拒绝。"""
        eng = self._make_engine()
        ctx = AgentContext()
        result = eng._spawn_all_subagents([{"engine": "react"}], ctx, None)
        assert "缺少 task" in result

    def test_spawn_all_valid_tasks_dispatched(self):
        """有效 task_list 正常分派（mock sub.run）。"""
        eng = self._make_engine()
        ctx = AgentContext()

        with patch.object(ReActEngine, 'run', return_value="子任务完成"):
            result = eng._spawn_all_subagents(
                [{"task": "task1", "engine": "react"}, {"task": "task2", "engine": "react"}],
                ctx, None,
            )

        assert "并行完成" in result
        assert "task1" in result or "子任务" in result.lower()

    # ── BUILTIN_TOOLS 注册 ──────────────────────────────

    def test_spawn_agent_in_tools(self):
        """spawn_agent 在 BUILTIN_TOOLS 中。"""
        assert "spawn_agent" in BUILTIN_TOOLS

    def test_spawn_agent_has_new_params(self):
        """spawn_agent 工具包含新参数（P0/P1/P2）。"""
        tool = BUILTIN_TOOLS["spawn_agent"]
        params = tool["params"]
        assert "task" in params
        assert "task_list" in params
        assert "engine" in params
        assert "timeout" in params

    # ── 格式化 ──────────────────────────────────────────

    def test_format_sub_result_success(self):
        """成功结果格式化。"""
        eng = self._make_engine()
        sub = MagicMock()
        sub._last_tracker = None
        result = eng._format_sub_result("id-1", "测试任务", "react", "任务完成", sub, None)
        assert "✅" in result
        assert "id-1" in result
        assert "react" in result

    def test_format_sub_result_timeout(self):
        """超时结果格式化。"""
        eng = self._make_engine()
        sub = MagicMock()
        sub._last_tracker = None
        result = eng._format_sub_result("id-1", "测试", "react", "执行超时（30s）", sub, None)
        assert "⏱️" in result or "超时" in result

    def test_format_sub_result_truncated(self):
        """过长结果被截断。"""
        eng = self._make_engine()
        sub = MagicMock()
        sub._last_tracker = None
        long_answer = "A" * 3000
        result = eng._format_sub_result("id-1", "测试", "react", long_answer, sub, None)
        assert "截断" in result

    def test_format_sub_result_tracks_to_parent(self):
        """结果记入父 tracker。"""
        eng = self._make_engine()
        sub = MagicMock()
        sub._last_tracker = None
        tracker = MagicMock()
        result = eng._format_sub_result("id-1", "测试", "react", "完成", sub, tracker)
        tracker.record.assert_called_once()

    # ── spawn_agent action_input 解析 ──────────────────

    def test_spawn_with_engine_type(self):
        """action_input 中的 engine 参数被正确读取。"""
        eng = self._make_engine()

        with patch.object(eng, '_build_sub_engine', return_value=MagicMock()):
            with patch.object(eng, '_format_sub_result', return_value="✅"):
                with patch.object(ReActEngine, 'run', return_value="ok"):
                    ctx = AgentContext()
                    result = eng._spawn_subagent(
                        {"task": "test", "engine": "plan_execute"}, ctx, None,
                    )
                    eng._build_sub_engine.assert_called_once_with("plan_execute", unittest.mock.ANY)
                    # 注意 'unittest' 需要在 pytest 环境也能工作

    def test_spawn_with_timeout(self):
        """action_input 中的 timeout 参数被正确读取。"""
        eng = self._make_engine(subagent_timeout=None)

        with patch('concurrent.futures.ThreadPoolExecutor') as mock_exec:
            mock_future = MagicMock()
            mock_future.result.return_value = "ok"
            mock_exec.return_value.__enter__.return_value.submit.return_value = mock_future

            with patch.object(eng, '_build_sub_engine', return_value=MagicMock()):
                ctx = AgentContext()
                eng._spawn_subagent(
                    {"task": "test", "timeout": 10}, ctx, None,
                )

            # 验证 ThreadPoolExecutor 被调用
            mock_exec.assert_called()

    # ── P1: 多引擎构建（8 种引擎）───────────────────

    # 已由 test_all_eight_engine_types_buildable 和 test_invalid_engine_rejected_with_all_options 覆盖
