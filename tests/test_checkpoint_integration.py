#!/usr/bin/env python3
"""
智能检查点 Phase 1 - 自动化集成测试脚本

包含正常场景、边界探索和变异测试（错误注入）。
测试 PartialResponseError 的完整工作流，包括异常情况。
"""

import sys
import time
from typing import Any
from unittest.mock import Mock, patch

import httpx

# 添加项目路径
sys.path.insert(0, "/home/xianyu-sheng/Xenon")

from xenon.engine.base import BaseEngine
from xenon.engine.callbacks import EngineCallback
from xenon.utils.partial_response import (
    ContinuationContext,
    PartialContent,
    PartialResponseError,
)


class TestResults:
    """测试结果收集器"""

    def __init__(self):
        self.total = 0
        self.passed = 0
        self.failed = 0
        self.errors = []

    def add_pass(self, name: str):
        self.total += 1
        self.passed += 1
        print(f"✅ PASS: {name}")

    def add_fail(self, name: str, reason: str):
        self.total += 1
        self.failed += 1
        self.errors.append((name, reason))
        print(f"❌ FAIL: {name}")
        print(f"   原因: {reason}")

    def summary(self):
        print("\n" + "=" * 70)
        print(f"测试总结: {self.passed}/{self.total} 通过")
        if self.failed > 0:
            print(f"\n失败的测试 ({self.failed}):")
            for name, reason in self.errors:
                print(f"  - {name}: {reason}")
        print("=" * 70)
        return self.failed == 0


results = TestResults()


# ============================================================
# 第一部分：边界探索测试
# ============================================================


def test_boundary_min_length():
    """边界测试：最小有效长度阈值"""
    print("\n[边界探索] 测试最小有效长度...")

    # 边界值：99 字符（刚好低于阈值）
    partial_99 = PartialContent(
        content="x" * 99,
        tokens_generated=25,
        model_id="test",
    )
    if not partial_99.is_valid(min_length=100):
        results.add_pass("边界-99字符不续写")
    else:
        results.add_fail("边界-99字符不续写", "应该返回 False 但返回了 True")

    # 边界值：100 字符（刚好达到阈值）
    partial_100 = PartialContent(
        content="x" * 100,
        tokens_generated=25,
        model_id="test",
    )
    if partial_100.is_valid(min_length=100):
        results.add_pass("边界-100字符续写")
    else:
        results.add_fail("边界-100字符续写", "应该返回 True 但返回了 False")

    # 边界值：101 字符（刚好超过阈值）
    partial_101 = PartialContent(
        content="x" * 101,
        tokens_generated=25,
        model_id="test",
    )
    if partial_101.is_valid(min_length=100):
        results.add_pass("边界-101字符续写")
    else:
        results.add_fail("边界-101字符续写", "应该返回 True 但返回了 False")


def test_boundary_empty_content():
    """边界测试：空内容"""
    print("\n[边界探索] 测试空内容...")

    partial_empty = PartialContent(
        content="",
        tokens_generated=0,
        model_id="test",
    )

    if len(partial_empty) == 0:
        results.add_pass("边界-空内容长度为0")
    else:
        results.add_fail("边界-空内容长度为0", f"长度应为 0 但为 {len(partial_empty)}")

    if not partial_empty.is_valid():
        results.add_pass("边界-空内容不续写")
    else:
        results.add_fail("边界-空内容不续写", "空内容不应该被续写")

    if partial_empty.estimate_tokens() >= 1:
        results.add_pass("边界-空内容tokens最小为1")
    else:
        results.add_fail("边界-空内容tokens最小为1", "estimate_tokens 应至少返回 1")


def test_boundary_huge_content():
    """边界测试：超大内容"""
    print("\n[边界探索] 测试超大内容...")

    # 100万字符
    huge_content = "x" * 1_000_000
    partial_huge = PartialContent(
        content=huge_content,
        tokens_generated=250_000,
        model_id="test",
    )

    if len(partial_huge) == 1_000_000:
        results.add_pass("边界-超大内容长度正确")
    else:
        results.add_fail("边界-超大内容长度正确", f"长度应为 1000000")

    if partial_huge.is_valid(min_length=100):
        results.add_pass("边界-超大内容可续写")
    else:
        results.add_fail("边界-超大内容可续写", "超大内容应该可以续写")


def test_boundary_unicode_mixed():
    """边界测试：复杂 Unicode 混合内容"""
    print("\n[边界探索] 测试复杂 Unicode...")

    # Emoji + 中文 + 英文 + 特殊字符
    mixed = "🎉测试Test✅💻代码Code🚀"
    partial_mixed = PartialContent(
        content=mixed,
        tokens_generated=0,
        model_id="test",
    )

    try:
        estimated = partial_mixed.estimate_tokens()
        if estimated > 0:
            results.add_pass("边界-Unicode混合tokens估算")
        else:
            results.add_fail("边界-Unicode混合tokens估算", "估算应 > 0")
    except Exception as e:
        results.add_fail("边界-Unicode混合tokens估算", f"抛出异常: {e}")


# ============================================================
# 第二部分：变异测试（错误注入）
# ============================================================


def test_mutation_invalid_tokens():
    """变异测试：无效的 tokens_generated 值"""
    print("\n[变异测试] 测试无效的 tokens_generated...")

    # 负数 tokens
    try:
        partial_negative = PartialContent(
            content="test content",
            tokens_generated=-100,  # 负数
            model_id="test",
        )
        # 应该接受负数但 estimate_tokens 返回 0（已知值）
        if partial_negative.estimate_tokens() == 100:  # abs(-100) = 100
            results.add_pass("变异-负数tokens被接受")
        else:
            # 实际上会忽略 tokens_generated，重新估算
            estimated = partial_negative.estimate_tokens()
            if estimated > 0:
                results.add_pass("变异-负数tokens回退到估算")
            else:
                results.add_fail("变异-负数tokens回退到估算", f"估算为 {estimated}")
    except Exception as e:
        results.add_fail("变异-负数tokens", f"不应抛出异常: {e}")

    # 超大 tokens
    try:
        partial_huge_tokens = PartialContent(
            content="short",
            tokens_generated=999_999_999,  # 接近 int 最大值
            model_id="test",
        )
        if partial_huge_tokens.estimate_tokens() == 999_999_999:
            results.add_pass("变异-超大tokens被接受")
        else:
            results.add_fail("变异-超大tokens", "应返回已知的 tokens_generated")
    except Exception as e:
        results.add_fail("变异-超大tokens", f"不应抛出异常: {e}")


def test_mutation_none_values():
    """变异测试：None 值"""
    print("\n[变异测试] 测试 None 值...")

    try:
        # finish_reason 可以是 None
        partial_none = PartialContent(
            content="test",
            tokens_generated=10,
            model_id="test",
            finish_reason=None,
        )
        if partial_none.finish_reason is None:
            results.add_pass("变异-finish_reason=None")
        else:
            results.add_fail("变异-finish_reason=None", "应接受 None")
    except Exception as e:
        results.add_fail("变异-finish_reason=None", f"不应抛出异常: {e}")


def test_mutation_special_strings():
    """变异测试：特殊字符串"""
    print("\n[变异测试] 测试特殊字符串...")

    special_cases = [
        ("SQL注入", "'; DROP TABLE users; --"),
        ("路径遍历", "../../../etc/passwd"),
        ("XSS", "<script>alert('xss')</script>"),
        ("null字节", "test\x00content"),
        ("超长模型ID", "x" * 10000),
    ]

    for name, value in special_cases:
        try:
            partial = PartialContent(
                content=value if "模型" not in name else "test",
                tokens_generated=10,
                model_id=value if "模型" in name else "test",
            )
            results.add_pass(f"变异-{name}")
        except Exception as e:
            results.add_fail(f"变异-{name}", f"抛出异常: {e}")


def test_mutation_error_types():
    """变异测试：各种原始错误类型"""
    print("\n[变异测试] 测试不同原始错误...")

    error_types = [
        ("ReadTimeout", httpx.ReadTimeout("timeout")),
        ("ConnectTimeout", httpx.ConnectTimeout("connect timeout")),
        ("RemoteProtocolError", httpx.RemoteProtocolError("protocol error")),
        ("Generic Exception", Exception("generic error")),
        ("RuntimeError", RuntimeError("runtime error")),
        ("ValueError", ValueError("value error")),
        ("None", None),
    ]

    partial = PartialContent(
        content="x" * 200,
        tokens_generated=50,
        model_id="test",
    )

    for name, error in error_types:
        try:
            exc = PartialResponseError(partial, error)
            if exc.original_error == error:
                results.add_pass(f"变异-错误类型-{name}")
            else:
                results.add_fail(f"变异-错误类型-{name}", "原始错误未保存")
        except Exception as e:
            results.add_fail(f"变异-错误类型-{name}", f"创建异常失败: {e}")


# ============================================================
# 第三部分：集成测试（模拟真实场景）
# ============================================================


def test_integration_continuation_workflow():
    """集成测试：完整续写工作流"""
    print("\n[集成测试] 测试完整续写工作流...")

    # 模拟网络中断场景
    partial = PartialContent(
        content="def fibonacci(n):\n    if n <= 1:\n        return n\n    ",
        tokens_generated=30,
        model_id="deepseek/deepseek-chat",
        finish_reason="network_timeout",
    )

    # 检查是否可续写
    if not partial.is_valid(min_length=50):
        results.add_fail("集成-续写判断", "应该可以续写但返回 False")
        return

    # 创建 PartialResponseError
    error = PartialResponseError(partial, httpx.ReadTimeout("timeout"))

    if not error.can_continue(min_length=50):
        results.add_fail("集成-PartialResponseError.can_continue", "应返回 True")
        return

    # 创建续写上下文
    ctx = ContinuationContext(
        original_model=partial.model_id,
        continuation_model="anthropic/claude-3-5-sonnet",
        partial_length=len(partial),
        partial_tokens=partial.estimate_tokens(),
        continuation_prompt="请继续完成函数",
    )

    # 模拟续写成功
    continuation = "return fibonacci(n-1) + fibonacci(n-2)"
    final_result = partial.content + continuation

    ctx.tokens_saved = partial.estimate_tokens()
    ctx.mark_completed(success=True)

    # 验证
    if "def fibonacci" in final_result and "return fibonacci(n-1)" in final_result:
        results.add_pass("集成-续写内容正确")
    else:
        results.add_fail("集成-续写内容正确", "最终结果不完整")

    if ctx.success:
        results.add_pass("集成-续写状态正确")
    else:
        results.add_fail("集成-续写状态正确", "success 应为 True")

    if ctx.tokens_saved > 0:
        results.add_pass("集成-tokens节省统计")
    else:
        results.add_fail("集成-tokens节省统计", "tokens_saved 应 > 0")


def test_integration_short_content_no_continuation():
    """集成测试：短内容不续写"""
    print("\n[集成测试] 测试短内容不续写...")

    partial_short = PartialContent(
        content="def ",
        tokens_generated=1,
        model_id="test",
    )

    error = PartialResponseError(partial_short)

    if not error.can_continue(min_length=100):
        results.add_pass("集成-短内容不续写")
    else:
        results.add_fail("集成-短内容不续写", "4字符不应续写")


def test_integration_context_tracking():
    """集成测试：上下文追踪和统计"""
    print("\n[集成测试] 测试上下文追踪...")

    ctx = ContinuationContext(
        original_model="model-a",
        continuation_model="model-b",
        partial_length=500,
        partial_tokens=120,
        continuation_prompt="测试提示",
    )

    # 测试耗时
    start_duration = ctx.duration()
    time.sleep(0.05)
    mid_duration = ctx.duration()

    if mid_duration > start_duration:
        results.add_pass("集成-耗时追踪增长")
    else:
        results.add_fail("集成-耗时追踪增长", "耗时应该增加")

    # 标记完成
    ctx.mark_completed(success=True)
    completed_duration = ctx.duration()
    time.sleep(0.05)
    after_complete = ctx.duration()

    if completed_duration == after_complete:
        results.add_pass("集成-完成后耗时固定")
    else:
        results.add_fail("集成-完成后耗时固定", "完成后耗时不应改变")

    # 测试 to_dict
    try:
        data = ctx.to_dict()
        required_keys = [
            "original_model",
            "continuation_model",
            "partial_length",
            "partial_tokens",
            "tokens_saved",
            "success",
            "duration",
        ]
        missing = [k for k in required_keys if k not in data]
        if not missing:
            results.add_pass("集成-to_dict完整性")
        else:
            results.add_fail("集成-to_dict完整性", f"缺少键: {missing}")
    except Exception as e:
        results.add_fail("集成-to_dict", f"抛出异常: {e}")


# ============================================================
# 第四部分：压力测试
# ============================================================


def test_stress_many_continuations():
    """压力测试：大量续写操作"""
    print("\n[压力测试] 测试大量续写...")

    try:
        contexts = []
        for i in range(100):
            ctx = ContinuationContext(
                original_model=f"model-{i}",
                continuation_model=f"model-{i+1}",
                partial_length=100 * i,
                partial_tokens=25 * i,
                continuation_prompt=f"prompt-{i}",
            )
            ctx.mark_completed(success=True)
            contexts.append(ctx)

        if len(contexts) == 100:
            results.add_pass("压力-100个续写上下文")
        else:
            results.add_fail("压力-100个续写上下文", f"创建了 {len(contexts)} 个")
    except Exception as e:
        results.add_fail("压力-100个续写上下文", f"抛出异常: {e}")


def test_stress_large_content():
    """压力测试：超大内容处理"""
    print("\n[压力测试] 测试超大内容...")

    try:
        # 10MB 内容
        huge = "x" * (10 * 1024 * 1024)
        partial = PartialContent(
            content=huge,
            tokens_generated=2_500_000,
            model_id="test",
        )

        # 测试基本操作
        if len(partial) == 10 * 1024 * 1024:
            results.add_pass("压力-10MB内容长度")
        else:
            results.add_fail("压力-10MB内容长度", "长度不匹配")

        # 测试续写判断（应该很快）
        start = time.time()
        is_valid = partial.is_valid()
        elapsed = time.time() - start

        if is_valid and elapsed < 0.1:
            results.add_pass("压力-10MB内容续写判断性能")
        else:
            results.add_fail("压力-10MB内容续写判断性能", f"耗时 {elapsed:.3f}s")
    except Exception as e:
        results.add_fail("压力-10MB内容", f"抛出异常: {e}")


# ============================================================
# 主函数
# ============================================================


def main():
    """运行所有测试"""
    print("=" * 70)
    print("智能检查点 Phase 1 - 自动化集成测试")
    print("包含边界探索、变异测试、集成测试和压力测试")
    print("=" * 70)

    # 边界探索
    print("\n" + "─" * 70)
    print("第一部分：边界探索测试")
    print("─" * 70)
    test_boundary_min_length()
    test_boundary_empty_content()
    test_boundary_huge_content()
    test_boundary_unicode_mixed()

    # 变异测试
    print("\n" + "─" * 70)
    print("第二部分：变异测试（错误注入）")
    print("─" * 70)
    test_mutation_invalid_tokens()
    test_mutation_none_values()
    test_mutation_special_strings()
    test_mutation_error_types()

    # 集成测试
    print("\n" + "─" * 70)
    print("第三部分：集成测试（真实场景）")
    print("─" * 70)
    test_integration_continuation_workflow()
    test_integration_short_content_no_continuation()
    test_integration_context_tracking()

    # 压力测试
    print("\n" + "─" * 70)
    print("第四部分：压力测试")
    print("─" * 70)
    test_stress_many_continuations()
    test_stress_large_content()

    # 总结
    success = results.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
