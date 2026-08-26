#!/usr/bin/env python3
"""
LLM 意图分类器真实调用测试。

测试场景：
1. 正则能识别的请求（应直接返回，不调用 LLM）
2. 正则无法识别的请求（应回退到 LLM 分类器）
3. 模糊输入（LLM 应返回 None）
4. 各种边界情况
"""

import logging
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from xenon.repl.difficulty_estimator import DifficultyEstimator
from xenon.repl.llm_intent_classifier import (
    get_llm_classifier,
    classify_intent_with_llm,
)
from xenon.repl.prompt_optimizer import detect_intent

# 配置日志
logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")

# 启用 LLM 分类器的 DEBUG 日志
logging.getLogger("xenon.repl.llm_intent_classifier").setLevel(logging.DEBUG)


def test_regex_classification():
    """测试正则分类器能识别的情况（不应调用 LLM）。"""
    print("\n" + "=" * 60)
    print("测试 1: 正则分类器能识别的请求")
    print("=" * 60)

    test_cases = [
        ("帮我写一个排序函数", "write_code"),
        ("这段代码报错了", "debug"),
        ("解释一下这段代码", "explain"),
        ("今天北京天气", "query"),
    ]

    for text, expected in test_cases:
        print(f"\n输入: {text}")

        # 1. 正则分类器
        regex_result = detect_intent(text)
        print(f"  正则分类: {regex_result}")

        # 2. 通过 DifficultyEstimator（集成了 LLM 回退）
        estimator = DifficultyEstimator()
        intent = estimator._detect_intent(text)
        print(f"  最终结果: {intent}")

        if regex_result == expected:
            print("  ✓ 正则分类正确")
        else:
            print(f"  ✗ 正则分类错误，期望 {expected}")


def test_llm_fallback():
    """测试正则无法识别、需要 LLM 回退的情况。"""
    print("\n" + "=" * 60)
    print("测试 2: 需要 LLM 回退的请求")
    print("=" * 60)

    # 这些是故意构造的、正则可能无法准确识别的请求
    test_cases = [
        "帮我搞定登录模块的性能问题",
        "需要一个配置文件",
        "把这个改得更好一些",
        "分析一下为什么会这样",
        "我想了解这个项目的架构",
    ]

    for text in test_cases:
        print(f"\n输入: {text}")

        # 1. 正则分类器
        regex_result = detect_intent(text)
        print(f"  正则分类: {regex_result}")

        # 2. 通过 DifficultyEstimator（集成了 LLM 回退）
        estimator = DifficultyEstimator()
        intent = estimator._detect_intent(text)
        print(f"  最终结果: {intent}")

        if regex_result is None and intent is not None:
            print(f"  ✓ LLM 回退成功，识别为: {intent}")
        elif regex_result is not None:
            print("  → 正则已识别，无需 LLM")
        else:
            print("  → 两者都无法识别")


def test_ambiguous_input():
    """测试模糊输入（期望返回 None）。"""
    print("\n" + "=" * 60)
    print("测试 3: 模糊输入")
    print("=" * 60)

    test_cases = [
        "嗯",
        "好的",
        "...",
        "？",
    ]

    for text in test_cases:
        print(f"\n输入: {text}")

        estimator = DifficultyEstimator()
        intent = estimator._detect_intent(text)
        print(f"  最终结果: {intent}")

        if intent is None:
            print("  ✓ 正确识别为无法判断")
        else:
            print("  ✗ 不应识别出意图")


def test_llm_classifier_direct():
    """直接调用 LLM 分类器测试。"""
    print("\n" + "=" * 60)
    print("测试 4: 直接调用 LLM 分类器")
    print("=" * 60)

    test_cases = [
        "重构缓存模块的并发逻辑",
        "收集用户对新功能的反馈",
        "看看这个函数有什么问题",
    ]

    classifier = get_llm_classifier()
    print("\n分类器配置:")
    print(f"  启用: {classifier.enabled}")
    print(f"  模型: {classifier.model}")
    print(f"  置信度阈值: {classifier.confidence_threshold}")
    print(f"  超时: {classifier.timeout}s")

    for text in test_cases:
        print(f"\n输入: {text}")

        result = classifier.classify(text)
        print(f"  意图: {result.intent}")
        print(f"  置信度: {result.confidence:.2f}")
        print(f"  理由: {result.reasoning}")
        print(f"  耗时: {result.latency_ms:.0f}ms")
        print(f"  降级: {result.fallback}")


def test_with_context():
    """测试带上下文的分类。"""
    print("\n" + "=" * 60)
    print("测试 5: 带上下文的分类")
    print("=" * 60)

    context = [
        {"role": "user", "content": "我在做一个电商系统"},
        {"role": "assistant", "content": "好的，需要什么帮助？"},
    ]

    text = "帮我处理订单模块"
    print(f"\n上下文: {context[0]['content']}")
    print(f"输入: {text}")

    result = classify_intent_with_llm(text, context_messages=context)
    print(f"  识别意图: {result}")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("LLM 意图分类器真实调用测试")
    print("=" * 60)

    try:
        # 测试 1: 正则能识别的
        test_regex_classification()

        # 测试 2: 需要 LLM 回退的
        test_llm_fallback()

        # 测试 3: 模糊输入
        test_ambiguous_input()

        # 测试 4: 直接调用 LLM
        test_llm_classifier_direct()

        # 测试 5: 带上下文
        test_with_context()

        print("\n" + "=" * 60)
        print("测试完成！")
        print("=" * 60)

    except KeyboardInterrupt:
        print("\n\n测试被中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n测试失败: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
