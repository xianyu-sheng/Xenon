#!/usr/bin/env python3
"""
Phase 2 变异测试 - 语义边界检测的边界探索和错误注入。

测试极端情况、错误输入和边界条件，确保系统鲁棒性。
"""

import sys

sys.path.insert(0, "/home/xianyu-sheng/Xenon")

from xenon.utils.semantic_boundary import (
    BoundaryDetector,
    CodeBoundaryDetector,
    ContentType,
    MarkdownBoundaryDetector,
    TextBoundaryDetector,
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
# 第一部分：边界探索 - 极端输入
# ============================================================


def test_boundary_empty_string():
    """边界探索：空字符串"""
    print("\n[边界探索] 测试空字符串...")
    detector = BoundaryDetector()

    result = detector.find_last_boundary("", ContentType.AUTO)
    if result is not None:
        if len(result.complete_part) == 0 and len(result.incomplete_part) == 0:
            results.add_pass("边界-空字符串返回空结果")
        else:
            results.add_fail("边界-空字符串", "应返回空内容")
    else:
        results.add_fail("边界-空字符串", "返回了 None")


def test_boundary_single_char():
    """边界探索：单字符"""
    print("\n[边界探索] 测试单字符...")
    detector = BoundaryDetector()

    for char in ["a", "中", ".", " ", "\n", "{", "}", "#"]:
        result = detector.find_last_boundary(char, ContentType.AUTO)
        if result is not None:
            results.add_pass(f"边界-单字符-{repr(char)}")
        else:
            results.add_fail(f"边界-单字符-{repr(char)}", "返回了 None")


def test_boundary_very_long_line():
    """边界探索：超长单行（无换行）"""
    print("\n[边界探索] 测试超长单行...")
    detector = BoundaryDetector()

    # 100万字符的单行
    long_line = "x" * 1_000_000
    result = detector.find_last_boundary(long_line, ContentType.AUTO)

    if result is not None and len(result.complete_part) > 0:
        results.add_pass("边界-100万字符单行")
    else:
        results.add_fail("边界-100万字符单行", "未正确处理超长单行")


def test_boundary_many_short_lines():
    """边界探索：大量短行"""
    print("\n[边界探索] 测试大量短行...")
    detector = BoundaryDetector()

    # 10000 行，每行 10 个字符
    many_lines = "\n".join(["line" + str(i) for i in range(10000)])
    result = detector.find_last_boundary(many_lines, ContentType.AUTO)

    if result is not None and len(result.complete_part) > 0:
        results.add_pass("边界-10000短行")
    else:
        results.add_fail("边界-10000短行", "未正确处理大量短行")


def test_boundary_only_whitespace():
    """边界探索：纯空白字符"""
    print("\n[边界探索] 测试纯空白字符...")
    detector = BoundaryDetector()

    whitespace_cases = [
        ("空格", " " * 100),
        ("换行", "\n" * 100),
        ("制表符", "\t" * 100),
        ("混合空白", "  \n\t  \n  " * 50),
    ]

    for name, text in whitespace_cases:
        result = detector.find_last_boundary(text, ContentType.AUTO)
        if result is not None:
            results.add_pass(f"边界-纯空白-{name}")
        else:
            results.add_fail(f"边界-纯空白-{name}", "返回了 None")


# ============================================================
# 第二部分：变异测试 - 格式错误
# ============================================================


def test_mutation_unbalanced_braces():
    """变异测试：不平衡的括号"""
    print("\n[变异测试] 测试不平衡括号...")
    detector = CodeBoundaryDetector()

    unbalanced_cases = [
        ("多左括号", "{{{"),
        ("多右括号", "}}}"),
        ("嵌套不平衡", "{{}{{"),
        ("混合不平衡", "({[})]"),
    ]

    for name, code in unbalanced_cases:
        try:
            _ = detector.detect(code)
            # 应该返回结果或 None，但不应该崩溃
            results.add_pass(f"变异-不平衡括号-{name}")
        except Exception as e:
            results.add_fail(f"变异-不平衡括号-{name}", f"抛出异常: {e}")


def test_mutation_incomplete_markdown():
    """变异测试：不完整的 Markdown"""
    print("\n[变异测试] 测试不完整 Markdown...")
    detector = MarkdownBoundaryDetector()

    incomplete_cases = [
        ("未闭合代码块", "```python\ndef foo():"),
        ("单个反引号", "`code"),
        ("双反引号", "``code"),
        ("未闭合标题", "#"),
        ("空标题", "## "),
    ]

    for name, text in incomplete_cases:
        try:
            _ = detector.detect(text)
            results.add_pass(f"变异-不完整Markdown-{name}")
        except Exception as e:
            results.add_fail(f"变异-不完整Markdown-{name}", f"抛出异常: {e}")


def test_mutation_malformed_code():
    """变异测试：畸形代码"""
    print("\n[变异测试] 测试畸形代码...")
    detector = CodeBoundaryDetector()

    malformed_cases = [
        ("缺少冒号", "def foo()"),
        ("缺少括号", "def foo:"),
        ("语法错误", "def def def"),
        ("混乱缩进", "def foo():\nreturn\n  x"),
    ]

    for name, code in malformed_cases:
        try:
            _ = detector.detect(code)
            results.add_pass(f"变异-畸形代码-{name}")
        except Exception as e:
            results.add_fail(f"变异-畸形代码-{name}", f"抛出异常: {e}")


def test_mutation_special_unicode():
    """变异测试：特殊 Unicode 字符"""
    print("\n[变异测试] 测试特殊 Unicode...")
    detector = BoundaryDetector()

    special_cases = [
        ("Emoji", "🎉🚀💻" * 100),
        ("零宽字符", "test​zero​width"),
        ("从右到左", "‏مرحبا‏"),
        ("组合字符", "é̀̂" * 50),
        ("全角标点", "。！？，；："),
        ("控制字符", "test\x00\x01\x02"),
    ]

    for name, text in special_cases:
        try:
            result = detector.find_last_boundary(text)
            if result is not None:
                results.add_pass(f"变异-特殊Unicode-{name}")
            else:
                results.add_fail(f"变异-特殊Unicode-{name}", "返回了 None")
        except Exception as e:
            results.add_fail(f"变异-特殊Unicode-{name}", f"抛出异常: {e}")


def test_mutation_nested_structures():
    """变异测试：深度嵌套结构"""
    print("\n[变异测试] 测试深度嵌套...")
    detector = CodeBoundaryDetector()

    # 100 层嵌套
    nested_code = "def f():\n" + ("    " * 100 + "return\n")

    try:
        _ = detector.detect(nested_code)
        results.add_pass("变异-100层嵌套代码")
    except Exception as e:
        results.add_fail("变异-100层嵌套代码", f"抛出异常: {e}")


# ============================================================
# 第三部分：变异测试 - 混淆输入
# ============================================================


def test_mutation_code_in_string():
    """变异测试：字符串中的代码"""
    print("\n[变异测试] 测试字符串中的代码...")
    detector = CodeBoundaryDetector()

    code_with_string = '''
def foo():
    s = """
    def fake_function():
        return 42
    """
    return s
incomplete
'''

    try:
        result = detector.detect(code_with_string)
        if result and "return s" in result.complete_part:
            results.add_pass("变异-字符串中的代码")
        else:
            results.add_fail("变异-字符串中的代码", "未正确处理字符串中的假代码")
    except Exception as e:
        results.add_fail("变异-字符串中的代码", f"抛出异常: {e}")


def test_mutation_comment_like_code():
    """变异测试：注释中的代码"""
    print("\n[变异测试] 测试注释中的代码...")
    detector = CodeBoundaryDetector()

    code_with_comment = '''
def foo():
    # def fake():
    #     return 1
    return 42
incomp
'''

    try:
        result = detector.detect(code_with_comment)
        if result and "return 42" in result.complete_part:
            results.add_pass("变异-注释中的代码")
        else:
            results.add_fail("变异-注释中的代码", "未正确处理注释")
    except Exception as e:
        results.add_fail("变异-注释中的代码", f"抛出异常: {e}")


def test_mutation_mixed_languages():
    """变异测试：混合编程语言"""
    print("\n[变异测试] 测试混合编程语言...")
    detector = BoundaryDetector()

    mixed_code = '''
# Python
def python_func():
    return 1

// JavaScript
function jsFunc() {
    return 2;
}

incomp
'''

    try:
        result = detector.find_last_boundary(mixed_code, ContentType.CODE)
        if result is not None:
            results.add_pass("变异-混合编程语言")
        else:
            results.add_fail("变异-混合编程语言", "返回了 None")
    except Exception as e:
        results.add_fail("变异-混合编程语言", f"抛出异常: {e}")


def test_mutation_markdown_in_code():
    """变异测试：代码中的 Markdown"""
    print("\n[变异测试] 测试代码中的 Markdown...")
    detector = BoundaryDetector()

    mixed = '''
def foo():
    doc = """
    # This is not a markdown header
    ```python
    # This is not code
    ```
    """
    return doc
incomp
'''

    try:
        result = detector.find_last_boundary(mixed, ContentType.AUTO)
        if result is not None:
            results.add_pass("变异-代码中的Markdown")
        else:
            results.add_fail("变异-代码中的Markdown", "返回了 None")
    except Exception as e:
        results.add_fail("变异-代码中的Markdown", f"抛出异常: {e}")


# ============================================================
# 第四部分：边界探索 - 回滚比例
# ============================================================


def test_boundary_rollback_ratio():
    """边界探索：回滚比例测试"""
    print("\n[边界探索] 测试回滚比例...")
    detector = BoundaryDetector()

    # 测试不同长度的不完整部分
    test_cases = [
        ("1%不完整", "a" * 99 + "。" + "b" * 1),
        ("10%不完整", "a" * 90 + "。" + "b" * 10),
        ("50%不完整", "a" * 50 + "。" + "b" * 50),
        ("90%不完整", "a" * 10 + "。" + "b" * 90),
        ("99%不完整", "a" * 1 + "。" + "b" * 99),
    ]

    for name, text in test_cases:
        result = detector.find_last_boundary(text, ContentType.TEXT)
        if result:
            ratio = result.rollback_ratio()
            # 回滚比例应该在合理范围内
            if 0 <= ratio <= 1:
                results.add_pass(f"边界-回滚比例-{name}")
            else:
                results.add_fail(f"边界-回滚比例-{name}", f"回滚比例异常: {ratio}")
        else:
            results.add_fail(f"边界-回滚比例-{name}", "返回了 None")


def test_boundary_no_valid_boundary():
    """边界探索：完全没有有效边界"""
    print("\n[边界探索] 测试无有效边界...")
    detector = BoundaryDetector()

    # 没有任何标点、空格、换行的长文本
    no_boundary = "abcdefghijklmnopqrstuvwxyz" * 100

    result = detector.find_last_boundary(no_boundary, ContentType.TEXT)
    if result is not None:
        # 应该返回保底方案（80% 截断或词边界）
        if len(result.complete_part) > 0:
            results.add_pass("边界-无有效边界保底方案")
        else:
            results.add_fail("边界-无有效边界保底方案", "完整部分为空")
    else:
        results.add_fail("边界-无有效边界", "返回了 None")


# ============================================================
# 第五部分：性能压力测试
# ============================================================


def test_stress_performance():
    """压力测试：检测性能"""
    print("\n[压力测试] 测试检测性能...")
    import time

    detector = BoundaryDetector()

    # 100KB 代码
    large_code = """
def function_{i}():
    x = 1
    y = 2
    return x + y

""" * 1000 + "incomp"

    start = time.time()
    result = detector.find_last_boundary(large_code, ContentType.CODE)
    elapsed = time.time() - start

    if result and elapsed < 0.1:  # 应该 < 100ms
        results.add_pass(f"压力-100KB代码性能 ({elapsed*1000:.1f}ms)")
    else:
        results.add_fail("压力-100KB代码性能", f"耗时 {elapsed:.3f}s 超过 0.1s")


def test_stress_many_boundaries():
    """压力测试：大量边界"""
    print("\n[压力测试] 测试大量边界...")
    detector = TextBoundaryDetector()

    # 10000 个句子
    many_sentences = "。".join([f"句子{i}" for i in range(10000)]) + "不完整"

    try:
        result = detector.detect(many_sentences)
        if result and "句子" in result.complete_part:
            results.add_pass("压力-10000句子")
        else:
            results.add_fail("压力-10000句子", "未找到有效边界")
    except Exception as e:
        results.add_fail("压力-10000句子", f"抛出异常: {e}")


# ============================================================
# 主函数
# ============================================================


def main():
    """运行所有变异测试"""
    print("=" * 70)
    print("Phase 2 语义边界检测 - 变异测试和边界探索")
    print("=" * 70)

    # 边界探索 - 极端输入
    print("\n" + "─" * 70)
    print("第一部分：边界探索 - 极端输入")
    print("─" * 70)
    test_boundary_empty_string()
    test_boundary_single_char()
    test_boundary_very_long_line()
    test_boundary_many_short_lines()
    test_boundary_only_whitespace()

    # 变异测试 - 格式错误
    print("\n" + "─" * 70)
    print("第二部分：变异测试 - 格式错误")
    print("─" * 70)
    test_mutation_unbalanced_braces()
    test_mutation_incomplete_markdown()
    test_mutation_malformed_code()
    test_mutation_special_unicode()
    test_mutation_nested_structures()

    # 变异测试 - 混淆输入
    print("\n" + "─" * 70)
    print("第三部分：变异测试 - 混淆输入")
    print("─" * 70)
    test_mutation_code_in_string()
    test_mutation_comment_like_code()
    test_mutation_mixed_languages()
    test_mutation_markdown_in_code()

    # 边界探索 - 回滚比例
    print("\n" + "─" * 70)
    print("第四部分：边界探索 - 回滚比例")
    print("─" * 70)
    test_boundary_rollback_ratio()
    test_boundary_no_valid_boundary()

    # 性能压力测试
    print("\n" + "─" * 70)
    print("第五部分：性能压力测试")
    print("─" * 70)
    test_stress_performance()
    test_stress_many_boundaries()

    # 总结
    success = results.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
