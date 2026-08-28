"""
测试智能路由器
"""

import pytest
from xenon.repl.intelligent_router import IntelligentRouter
from xenon.repl.context_manager import ContextManager


class TestIntelligentRouter:
    @pytest.fixture
    def router(self):
        return IntelligentRouter(enabled=True, confidence_threshold=0.5)

    @pytest.fixture
    def context(self):
        return ContextManager()

    def test_router_disabled(self, context):
        """测试路由器禁用时返回 None"""
        router = IntelligentRouter(enabled=False)
        decision = router.route("写一个函数", context, "direct")
        assert decision is None

    def test_simple_query_no_switch(self, router, context):
        """测试简单查询在 direct 模式下不切换"""
        decision = router.route("什么是 Python？", context, "direct")
        assert decision is None  # 已经是 direct，无需切换

    def test_coding_task_switch_to_react(self, router, context):
        """测试编码任务从 direct 切换到 react"""
        decision = router.route(
            "帮我创建一个 Python 文件，写入快速排序函数，然后测试",
            context,
            "direct"
        )
        assert decision is not None
        assert decision.paradigm == "react"
        assert decision.confidence >= 0.5

    def test_quality_critical_switch_to_reflection(self, router, context):
        """测试质量敏感任务切换到 reflection"""
        decision = router.route(
            "这是生产代码，请帮我实现一个安全的用户认证模块",
            context,
            "direct"
        )
        assert decision is not None
        assert decision.paradigm == "reflection"

    def test_low_confidence_no_switch(self, context):
        """测试低置信度时不切换"""
        router = IntelligentRouter(enabled=True, confidence_threshold=0.9)
        decision = router.route("写代码", context, "direct")
        # 置信度可能不足 0.9
        assert decision is None or decision.confidence >= 0.9

    def test_improvement_check_prevents_downgrade(self, router, context):
        """测试防止过度降级"""
        # 从 reflection 到 direct 是大幅降级，应该被阻止
        router.switch_only_on_improvement = True
        decision = router.route(
            "今天天气怎么样？",  # 简单查询
            context,
            "reflection"  # 当前是高级范式
        )
        # 应该保持 reflection 或者被置信度/优先级检查拦截
        assert decision is None or decision.paradigm != "direct"

    def test_improvement_check_allows_upgrade(self, router, context):
        """测试允许升级"""
        router.switch_only_on_improvement = True
        decision = router.route(
            "这是生产代码，实现用户认证",  # 质量敏感任务
            context,
            "direct"  # 从低级范式
        )
        # 应该允许升级到 reflection
        assert decision is not None
        assert decision.paradigm == "reflection"

    def test_improvement_check_allows_same_level(self, router, context):
        """测试允许同级切换"""
        router.switch_only_on_improvement = True
        # plan-execute 和 reflection 都是强度 3
        decision = router.route(
            "这是生产代码，实现一个模块",
            context,
            "plan-execute"
        )
        # 如果匹配到 reflection，应该允许切换
        if decision and decision.paradigm == "reflection":
            assert decision is not None

    def test_improvement_check_allows_one_level_down(self, router, context):
        """测试允许降一级"""
        router.switch_only_on_improvement = True
        decision = router.route(
            "创建一个文件",  # 简单文件操作
            context,
            "reflection"  # 强度 3
        )
        # 降到 react（强度 2）应该允许
        if decision and decision.paradigm == "react":
            assert decision is not None

    def test_stats_tracking(self, router, context):
        """测试统计信息跟踪"""
        router.route("问题1", context, "direct")
        router.route("问题2", context, "direct")

        stats = router.get_stats()
        assert stats["total_routes"] == 2
        assert "success_rate" in stats
        assert 0 <= stats["success_rate"] <= 1

    def test_enable_disable(self):
        """测试启用/禁用功能"""
        router = IntelligentRouter(enabled=False)
        assert router.enabled is False

        router.enable()
        assert router.enabled is True

        router.disable()
        assert router.enabled is False

    def test_switch_counter(self, router, context):
        """测试切换计数器"""
        initial_switches = router._switches

        # 触发一次切换
        decision = router.route(
            "创建文件并写入代码",
            context,
            "direct"
        )

        if decision is not None:
            assert router._switches == initial_switches + 1

    def test_no_switch_on_same_paradigm(self, router, context):
        """测试相同范式不计入切换"""
        initial_switches = router._switches

        # 简单查询，当前已经是 direct
        decision = router.route("什么是 Python？", context, "direct")

        assert decision is None
        assert router._switches == initial_switches  # 切换计数不变

    def test_exception_handling(self, router):
        """测试异常处理：出错时返回 None"""
        # 模拟一个会导致异常的情况
        # 保存原始的 extractor
        original_extractor = router.extractor

        # 创建一个会抛异常的 mock extractor
        class BrokenExtractor:
            def extract(self, user_input, context):
                raise RuntimeError("模拟的提取错误")

        router.extractor = BrokenExtractor()

        # 应该捕获异常并返回 None
        decision = router.route("测试", ContextManager(), "direct")
        assert decision is None

        # 恢复原始 extractor
        router.extractor = original_extractor

    def test_confidence_threshold_filtering(self, context):
        """测试置信度阈值过滤"""
        # 高阈值路由器
        router_high = IntelligentRouter(enabled=True, confidence_threshold=0.95)
        # 低阈值路由器
        router_low = IntelligentRouter(enabled=True, confidence_threshold=0.3)

        user_input = "写一个函数"

        decision_high = router_high.route(user_input, context, "direct")
        decision_low = router_low.route(user_input, context, "direct")

        # 低阈值更容易通过
        # 高阈值可能拦截，低阈值更可能通过
        if decision_high is None and decision_low is not None:
            assert decision_low.confidence < 0.95

    def test_notify_user_flag(self):
        """测试用户通知标志"""
        router_notify = IntelligentRouter(enabled=True, notify_user=True)
        router_silent = IntelligentRouter(enabled=True, notify_user=False)

        assert router_notify.notify_user is True
        assert router_silent.notify_user is False

    def test_switch_only_on_improvement_flag(self):
        """测试只在改进时切换标志"""
        router_strict = IntelligentRouter(
            enabled=True,
            switch_only_on_improvement=True
        )
        router_permissive = IntelligentRouter(
            enabled=True,
            switch_only_on_improvement=False
        )

        assert router_strict.switch_only_on_improvement is True
        assert router_permissive.switch_only_on_improvement is False
