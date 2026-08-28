"""
测试范式选择器
"""

import pytest
from xenon.repl.paradigm_selector import ParadigmSelector, RoutingRule
from xenon.repl.feature_extractor import TaskFeatures


class TestParadigmSelector:
    @pytest.fixture
    def selector(self):
        return ParadigmSelector()

    def test_simple_query_to_direct(self, selector):
        """测试简单查询路由到 direct"""
        features = TaskFeatures(
            is_query=True,
            is_coding=False,
            is_exploratory=False,
            input_length=30,
            estimated_steps=1,
            estimated_tool_calls=0,
            complexity_score=0.1,
            has_file_operations=False,
            has_git_operations=False,
            needs_web_access=False,
            quality_critical=False,
        )
        decision = selector.select(features)
        assert decision.paradigm == "direct"
        assert decision.matched_rule == "R1"
        assert decision.confidence == 0.9

    def test_multi_tool_to_react(self, selector):
        """测试多工具调用路由到 react"""
        features = TaskFeatures(
            is_query=False,
            is_coding=True,
            is_exploratory=False,
            input_length=100,
            estimated_steps=3,
            estimated_tool_calls=5,
            complexity_score=0.5,
            has_file_operations=True,
            has_git_operations=False,
            needs_web_access=False,
            quality_critical=False,
        )
        decision = selector.select(features)
        assert decision.paradigm == "react"
        assert decision.matched_rule == "R4"

    def test_quality_critical_code_to_reflection(self, selector):
        """测试质量敏感代码路由到 reflection"""
        features = TaskFeatures(
            is_query=False,
            is_coding=True,
            is_exploratory=False,
            input_length=150,
            estimated_steps=2,
            estimated_tool_calls=2,
            complexity_score=0.4,
            has_file_operations=True,
            has_git_operations=False,
            needs_web_access=False,
            quality_critical=True,
        )
        decision = selector.select(features)
        assert decision.paradigm == "reflection"
        assert decision.matched_rule == "R2"

    def test_complex_multi_step_to_plan_execute(self, selector):
        """测试复杂多步骤路由到 plan-execute"""
        features = TaskFeatures(
            is_query=False,
            is_coding=False,
            is_exploratory=False,
            input_length=300,
            estimated_steps=6,
            estimated_tool_calls=2,
            complexity_score=0.7,
            has_file_operations=True,
            has_git_operations=True,
            needs_web_access=False,
            quality_critical=False,
        )
        decision = selector.select(features)
        assert decision.paradigm == "plan-execute"
        assert decision.matched_rule == "R5"

    def test_exploratory_quality_to_react_reflection(self, selector):
        """测试探索性+质量敏感路由到 react-reflection"""
        features = TaskFeatures(
            is_query=False,
            is_coding=False,  # 不是编码任务，避免被 R2 拦截
            is_exploratory=True,
            input_length=200,
            estimated_steps=4,
            estimated_tool_calls=4,
            complexity_score=0.6,
            has_file_operations=True,
            has_git_operations=False,
            needs_web_access=False,
            quality_critical=True,
        )
        decision = selector.select(features)
        assert decision.paradigm == "react-reflection"
        assert decision.matched_rule == "R3"

    def test_rule_priority(self, selector):
        """测试规则优先级：高优先级规则先匹配"""
        # R2 (优先级90) 应该比 R6 (优先级50) 先匹配
        features = TaskFeatures(
            is_query=False,
            is_coding=True,
            is_exploratory=False,
            input_length=100,
            estimated_steps=2,
            estimated_tool_calls=1,
            complexity_score=0.4,
            has_file_operations=True,
            has_git_operations=False,
            needs_web_access=False,
            quality_critical=True,  # 同时满足 R2 和 R6
        )
        decision = selector.select(features)
        assert decision.paradigm == "reflection"  # R2 优先
        assert decision.matched_rule == "R2"

    def test_file_operations_to_react(self, selector):
        """测试文件操作路由到 react"""
        features = TaskFeatures(
            is_query=False,
            is_coding=False,
            is_exploratory=False,
            input_length=50,
            estimated_steps=2,
            estimated_tool_calls=1,
            complexity_score=0.2,
            has_file_operations=True,
            has_git_operations=False,
            needs_web_access=False,
            quality_critical=False,
        )
        decision = selector.select(features)
        assert decision.paradigm == "react"
        assert decision.matched_rule == "R7"

    def test_medium_complexity_to_react(self, selector):
        """测试中等复杂度路由到 react"""
        features = TaskFeatures(
            is_query=False,
            is_coding=False,
            is_exploratory=False,
            input_length=200,
            estimated_steps=2,
            estimated_tool_calls=1,
            complexity_score=0.45,  # 在 0.3-0.6 之间
            has_file_operations=False,
            has_git_operations=False,
            needs_web_access=False,
            quality_critical=False,
        )
        decision = selector.select(features)
        assert decision.paradigm == "react"
        assert decision.matched_rule == "R8"

    def test_fallback_to_direct(self, selector):
        """测试兜底到 direct"""
        features = TaskFeatures(
            is_query=False,
            is_coding=False,
            is_exploratory=False,
            input_length=10,
            estimated_steps=1,
            estimated_tool_calls=0,
            complexity_score=0.05,  # 很低
            has_file_operations=False,
            has_git_operations=False,
            needs_web_access=False,
            quality_critical=False,
        )
        decision = selector.select(features)
        assert decision.paradigm == "direct"
        assert decision.matched_rule == "R9"

    def test_custom_rule(self, selector):
        """测试添加自定义规则"""
        custom_rule = RoutingRule(
            rule_id="CUSTOM1",
            description="测试自定义规则",
            condition=lambda f: f.needs_web_access,
            paradigm="react",
            confidence=0.95,
            reason="需要网络访问",
            priority=95  # 高优先级
        )
        selector.add_rule(custom_rule)

        features = TaskFeatures(
            is_query=False,  # 不是查询，避免被 R1 拦截
            is_coding=False,
            is_exploratory=False,
            input_length=50,
            estimated_steps=1,
            estimated_tool_calls=0,
            complexity_score=0.2,
            has_file_operations=False,
            has_git_operations=False,
            needs_web_access=True,  # 触发自定义规则
            quality_critical=False,
        )
        decision = selector.select(features)
        assert decision.paradigm == "react"
        assert decision.matched_rule == "CUSTOM1"

    def test_rule_exception_handling(self, selector):
        """测试规则异常处理：坏规则不应阻止后续匹配"""
        # 添加一个会抛异常的规则
        bad_rule = RoutingRule(
            rule_id="BAD",
            description="会抛异常的规则",
            condition=lambda f: 1 / 0,  # 除零错误
            paradigm="direct",
            confidence=0.99,
            reason="不应该被使用",
            priority=99  # 非常高优先级
        )
        selector.add_rule(bad_rule)

        # 应该跳过坏规则，继续匹配后续规则
        features = TaskFeatures(
            is_query=True,
            is_coding=False,
            is_exploratory=False,
            input_length=30,
            estimated_steps=1,
            estimated_tool_calls=0,
            complexity_score=0.1,
            has_file_operations=False,
            has_git_operations=False,
            needs_web_access=False,
            quality_critical=False,
        )
        decision = selector.select(features)
        # 应该匹配 R1 而不是坏规则
        assert decision.paradigm == "direct"
        assert decision.matched_rule == "R1"

    def test_list_rules(self, selector):
        """测试列出所有规则"""
        rules = selector.list_rules()
        assert len(rules) == 9  # 9条内置规则
        # 验证按优先级排序
        priorities = [r.priority for r in rules]
        assert priorities == sorted(priorities, reverse=True)

    def test_no_match_fallback(self, selector):
        """测试没有规则匹配时的兜底"""
        # 清空所有规则
        selector.rules = []

        features = TaskFeatures(
            is_query=False,
            is_coding=False,
            is_exploratory=False,
            input_length=100,
            estimated_steps=1,
            estimated_tool_calls=0,
            complexity_score=0.5,
            has_file_operations=False,
            has_git_operations=False,
            needs_web_access=False,
            quality_critical=False,
        )
        decision = selector.select(features)
        assert decision.paradigm == "direct"
        assert decision.matched_rule is None
        assert decision.confidence == 0.3
