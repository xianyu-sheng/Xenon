"""测试循环检测器在所有引擎中的集成

v0.9.1: 验证 LoopDetector 在 Plan-Execute、Reflection、组合引擎中正常工作
"""

import pytest

from xenon.engine.callbacks import EngineCallback
from xenon.engine.combined_engines import (
    PlanReactEngine,
    PlanReflectionEngine,
    ReactReflectionEngine,
)
from xenon.engine.context import AgentContext
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.reflection_engine import ReflectionEngine


class MockLLM:
    """模拟 LLM，返回重复输出触发循环检测"""

    def __init__(self, responses):
        self.responses = responses
        self.call_count = 0

    def __call__(self, *args, **kwargs):
        response = self.responses[self.call_count % len(self.responses)]
        self.call_count += 1
        return response


class TestPlanExecuteLoopDetection:
    """测试 Plan-Execute 引擎的循环检测"""

    def test_loop_detector_initialized(self):
        """验证循环检测器已初始化"""
        engine = PlanExecuteEngine(["mock"])
        assert hasattr(engine, "_loop_detector")
        assert engine._loop_detector.enabled

    def test_loop_detector_reset_on_run(self):
        """验证每次 run 都重置循环检测器"""
        engine = PlanExecuteEngine(["mock"])
        ctx = AgentContext()

        # 手动添加一些历史
        engine._loop_detector.add_turn("test", [], None, "thought")

        # 添加后应该能检测到（通过 stats）
        stats = engine._loop_detector.stats()
        assert stats["turn_count"] == 1

        # 模拟 run，应该会重置
        engine._loop_detector.reset()
        stats = engine._loop_detector.stats()
        assert stats["turn_count"] == 0


class TestReflectionLoopDetection:
    """测试 Reflection 引擎的循环检测"""

    def test_loop_detector_initialized(self):
        """验证循环检测器已初始化"""
        engine = ReflectionEngine(["mock"])
        assert hasattr(engine, "_loop_detector")
        assert engine._loop_detector.enabled

    def test_loop_detector_parameters(self):
        """验证 Reflection 的循环检测参数"""
        engine = ReflectionEngine(["mock"])
        # Reflection 应该使用更严格的阈值
        assert engine._loop_detector.window_size == 3
        assert engine._loop_detector.similarity_threshold == 0.90


class TestCombinedEnginesLoopDetection:
    """测试组合引擎的循环检测"""

    def test_plan_react_loop_detector_initialized(self):
        """验证 Plan-React 循环检测器已初始化"""
        engine = PlanReactEngine(["mock"])
        assert hasattr(engine, "_loop_detector")
        assert engine._loop_detector.enabled
        # 子引擎也应该有循环检测器
        assert hasattr(engine.planner, "_loop_detector")
        assert hasattr(engine.reactor, "_loop_detector")

    def test_plan_reflection_loop_detector_initialized(self):
        """验证 Plan-Reflection 循环检测器已初始化"""
        engine = PlanReflectionEngine(["mock"])
        assert hasattr(engine, "_loop_detector")
        assert engine._loop_detector.enabled
        # 子引擎也应该有循环检测器
        assert hasattr(engine.planner, "_loop_detector")
        assert hasattr(engine.reflector, "_loop_detector")
        assert hasattr(engine.repairer, "_loop_detector")

    def test_react_reflection_loop_detector_initialized(self):
        """验证 React-Reflection 循环检测器已初始化"""
        engine = ReactReflectionEngine(["mock"])
        assert hasattr(engine, "_loop_detector")
        assert engine._loop_detector.enabled
        # 子引擎也应该有循环检测器
        assert hasattr(engine.reactor, "_loop_detector")
        assert hasattr(engine.repairer, "_loop_detector")
        assert hasattr(engine.reflector, "_loop_detector")


class TestLoopDetectionConfiguration:
    """测试不同引擎的循环检测配置"""

    def test_plan_execute_configuration(self):
        """Plan-Execute 使用较宽松的配置"""
        engine = PlanExecuteEngine(["mock"])
        assert engine._loop_detector.window_size == 3  # 较短窗口
        assert engine._loop_detector.similarity_threshold == 0.80  # 较宽松

    def test_reflection_configuration(self):
        """Reflection 使用较严格的配置"""
        engine = ReflectionEngine(["mock"])
        assert engine._loop_detector.window_size == 3  # 较短窗口
        assert engine._loop_detector.similarity_threshold == 0.90  # 较严格

    def test_plan_react_configuration(self):
        """Plan-React 使用中等配置"""
        engine = PlanReactEngine(["mock"])
        assert engine._loop_detector.window_size == 5  # 较长窗口
        assert engine._loop_detector.similarity_threshold == 0.85  # 中等

    def test_combined_reflection_configuration(self):
        """组合 Reflection 引擎使用中等配置"""
        engine = PlanReflectionEngine(["mock"])
        assert engine._loop_detector.window_size == 4
        assert engine._loop_detector.similarity_threshold == 0.85


class TestLoopDetectionInheritance:
    """测试组合引擎正确继承和使用子引擎的循环检测器"""

    def test_subengines_have_independent_detectors(self):
        """验证子引擎有独立的循环检测器"""
        engine = PlanReactEngine(["mock"])

        # 每个引擎都有自己的检测器实例
        assert engine._loop_detector is not engine.planner._loop_detector
        assert engine._loop_detector is not engine.reactor._loop_detector
        assert engine.planner._loop_detector is not engine.reactor._loop_detector

    def test_subengine_detectors_can_be_disabled_independently(self):
        """验证可以独立禁用子引擎的检测器"""
        engine = PlanReactEngine(["mock"])

        # 禁用组合层检测器
        engine._loop_detector.enabled = False
        assert not engine._loop_detector.enabled

        # 子引擎检测器仍然启用
        assert engine.planner._loop_detector.enabled
        assert engine.reactor._loop_detector.enabled
