"""
Phase 1 单元测试 - 部分响应和续写机制。

测试 PartialContent、PartialResponseError 和 ContinuationContext 的基本功能。
"""

import time

from xenon.utils.partial_response import (
    ContinuationContext,
    PartialContent,
    PartialResponseError,
)


class TestPartialContent:
    """测试 PartialContent 数据结构"""

    def test_basic_creation(self):
        """测试基本创建"""
        partial = PartialContent(
            content="这是部分生成的内容",
            tokens_generated=50,
            model_id="deepseek/deepseek-chat",
            finish_reason="network_error",
        )

        assert partial.content == "这是部分生成的内容"
        assert partial.tokens_generated == 50
        assert partial.model_id == "deepseek/deepseek-chat"
        assert partial.finish_reason == "network_error"
        assert len(partial) == 9  # 字符长度（9个汉字）

    def test_is_valid_default_threshold(self):
        """测试默认阈值 100 字符"""
        short = PartialContent(
            content="短内容",
            tokens_generated=10,
            model_id="test",
        )
        assert not short.is_valid()  # 5 字符 < 100

        long = PartialContent(
            content="x" * 100,
            tokens_generated=25,
            model_id="test",
        )
        assert long.is_valid()  # 100 字符 >= 100

    def test_is_valid_custom_threshold(self):
        """测试自定义阈值"""
        partial = PartialContent(
            content="中等长度的内容",
            tokens_generated=10,
            model_id="test",
        )

        assert partial.is_valid(min_length=5)  # 7 字符 >= 5
        assert not partial.is_valid(min_length=10)  # 7 字符 < 10

    def test_estimate_tokens_with_generated(self):
        """测试已知 tokens_generated 时直接返回"""
        partial = PartialContent(
            content="任意内容",
            tokens_generated=100,
            model_id="test",
        )
        assert partial.estimate_tokens() == 100

    def test_estimate_tokens_chinese(self):
        """测试中文 token 估算（约 2 字符/token）"""
        partial = PartialContent(
            content="这是一段中文内容，共二十个汉字。",
            tokens_generated=0,
            model_id="test",
        )
        # 16 个中文字符 / 2 ≈ 8 tokens
        estimated = partial.estimate_tokens()
        assert 7 <= estimated <= 10  # 允许误差

    def test_estimate_tokens_english(self):
        """测试英文 token 估算（约 4 字符/token）"""
        partial = PartialContent(
            content="This is a test sentence with multiple words.",
            tokens_generated=0,
            model_id="test",
        )
        # 45 个字符 / 4 ≈ 11 tokens
        estimated = partial.estimate_tokens()
        assert 10 <= estimated <= 13

    def test_estimate_tokens_mixed(self):
        """测试中英文混合 token 估算"""
        partial = PartialContent(
            content="这是中文 and this is English，混合内容。",
            tokens_generated=0,
            model_id="test",
        )
        # 约 4 个中文 (2 tokens) + 约 20 个英文 (5 tokens) ≈ 7 tokens
        estimated = partial.estimate_tokens()
        assert estimated >= 5  # 至少有一些 tokens

    def test_metadata(self):
        """测试元数据字段"""
        partial = PartialContent(
            content="test",
            tokens_generated=10,
            model_id="test",
            metadata={"checkpoint_id": 1, "boundary_type": "code_block"},
        )
        assert partial.metadata["checkpoint_id"] == 1
        assert partial.metadata["boundary_type"] == "code_block"


class TestPartialResponseError:
    """测试 PartialResponseError 异常"""

    def test_basic_creation(self):
        """测试基本创建"""
        partial = PartialContent(
            content="部分内容",
            tokens_generated=20,
            model_id="test/model",
            finish_reason="timeout",
        )
        error = PartialResponseError(partial)

        assert error.partial == partial
        assert error.original_error is None
        assert "test/model" in str(error)
        assert "4" in str(error)  # 4 个字符

    def test_with_original_error(self):
        """测试携带原始异常"""
        import httpx

        partial = PartialContent(
            content="x" * 200,
            tokens_generated=50,
            model_id="test/model",
        )
        original = httpx.ReadTimeout("timeout")
        error = PartialResponseError(partial, original)

        assert error.original_error == original
        assert isinstance(error.original_error, httpx.ReadTimeout)

    def test_can_continue_valid(self):
        """测试可以续写的情况"""
        partial = PartialContent(
            content="x" * 150,
            tokens_generated=40,
            model_id="test",
        )
        error = PartialResponseError(partial)
        assert error.can_continue(min_length=100)

    def test_can_continue_invalid(self):
        """测试不能续写的情况"""
        partial = PartialContent(
            content="x" * 50,
            tokens_generated=10,
            model_id="test",
        )
        error = PartialResponseError(partial)
        assert not error.can_continue(min_length=100)

    def test_repr(self):
        """测试字符串表示"""
        partial = PartialContent(
            content="test",
            tokens_generated=1,
            model_id="model-a",
            finish_reason="network",
        )
        error = PartialResponseError(partial)
        repr_str = repr(error)

        assert "PartialResponseError" in repr_str
        assert "model-a" in repr_str
        assert "length=4" in repr_str
        assert "network" in repr_str


class TestContinuationContext:
    """测试 ContinuationContext 续写上下文"""

    def test_basic_creation(self):
        """测试基本创建"""
        ctx = ContinuationContext(
            original_model="model-a",
            continuation_model="model-b",
            partial_length=200,
            partial_tokens=50,
            continuation_prompt="请继续",
        )

        assert ctx.original_model == "model-a"
        assert ctx.continuation_model == "model-b"
        assert ctx.partial_length == 200
        assert ctx.partial_tokens == 50
        assert ctx.continuation_prompt == "请继续"
        assert not ctx.success
        assert ctx.error is None

    def test_mark_completed_success(self):
        """测试标记成功完成"""
        ctx = ContinuationContext(
            original_model="a",
            continuation_model="b",
            partial_length=100,
            partial_tokens=25,
            continuation_prompt="test",
        )

        assert ctx.completed_at is None

        ctx.mark_completed(success=True)

        assert ctx.success
        assert ctx.completed_at is not None
        assert ctx.error is None

    def test_mark_completed_failure(self):
        """测试标记失败完成"""
        ctx = ContinuationContext(
            original_model="a",
            continuation_model="b",
            partial_length=100,
            partial_tokens=25,
            continuation_prompt="test",
        )

        ctx.mark_completed(success=False, error="续写失败")

        assert not ctx.success
        assert ctx.error == "续写失败"
        assert ctx.completed_at is not None

    def test_duration(self):
        """测试耗时计算"""
        ctx = ContinuationContext(
            original_model="a",
            continuation_model="b",
            partial_length=100,
            partial_tokens=25,
            continuation_prompt="test",
        )

        # 未完成时，返回当前经过的时间
        time.sleep(0.05)
        duration1 = ctx.duration()
        assert duration1 >= 0.05

        # 完成后，返回固定耗时
        ctx.mark_completed(success=True)
        duration2 = ctx.duration()
        time.sleep(0.05)
        duration3 = ctx.duration()
        assert duration3 == duration2  # 完成后耗时不变

    def test_to_dict(self):
        """测试转换为字典"""
        ctx = ContinuationContext(
            original_model="deepseek/chat",
            continuation_model="anthropic/sonnet",
            partial_length=500,
            partial_tokens=120,
            continuation_prompt="这是一个很长的续写提示语" * 10,
            tokens_saved=100,
        )
        ctx.mark_completed(success=True)

        result = ctx.to_dict()

        assert result["original_model"] == "deepseek/chat"
        assert result["continuation_model"] == "anthropic/sonnet"
        assert result["partial_length"] == 500
        assert result["partial_tokens"] == 120
        assert result["tokens_saved"] == 100
        assert result["success"] is True
        assert result["error"] is None
        assert "duration" in result

        # 检查提示语被截断（50 字符 + "..."）
        assert len(result["continuation_prompt"]) == 53
        assert result["continuation_prompt"].endswith("...")


class TestIntegration:
    """集成测试 - 模拟完整的续写流程"""

    def test_full_continuation_workflow(self):
        """测试完整的续写工作流"""
        # 1. 模拟第一个模型生成部分内容后中断
        partial = PartialContent(
            content="def calculate_fibonacci(n):\n    if n <= 1:\n        return n\n    ",
            tokens_generated=30,
            model_id="deepseek/deepseek-chat",
            finish_reason="network_timeout",
        )

        assert partial.is_valid(min_length=50)  # 64 字符 > 50

        # 2. 抛出 PartialResponseError
        error = PartialResponseError(partial, Exception("network timeout"))
        assert error.can_continue(min_length=50)  # 明确指定阈值

        # 3. 创建续写上下文
        ctx = ContinuationContext(
            original_model=partial.model_id,
            continuation_model="<pending>",
            partial_length=len(partial),
            partial_tokens=partial.estimate_tokens(),
            continuation_prompt="请继续完成函数",
        )

        # 4. 模拟续写成功
        continuation_result = "return calculate_fibonacci(n-1) + calculate_fibonacci(n-2)"
        final_result = partial.content + continuation_result

        ctx.continuation_model = "anthropic/claude-3-5-sonnet"
        ctx.tokens_saved = partial.estimate_tokens()
        ctx.mark_completed(success=True)

        # 5. 验证最终结果
        assert "def calculate_fibonacci" in final_result
        assert "return n" in final_result
        assert "calculate_fibonacci(n-1)" in final_result
        assert ctx.success
        assert ctx.tokens_saved > 0

    def test_short_partial_no_continuation(self):
        """测试部分内容过短时不续写"""
        partial = PartialContent(
            content="def ",
            tokens_generated=1,
            model_id="test",
        )

        error = PartialResponseError(partial)
        assert not error.can_continue(min_length=100)

        # 应该重新生成，而不是续写
