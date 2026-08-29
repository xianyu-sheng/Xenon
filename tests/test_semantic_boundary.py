"""
Phase 2 单元测试 - 语义边界检测。

测试 BoundaryDetector、CodeBoundaryDetector、MarkdownBoundaryDetector
和 TextBoundaryDetector 的各种边界检测功能。
"""

from xenon.utils.semantic_boundary import (
    BoundaryDetector,
    BoundaryResult,
    BoundaryType,
    CodeBoundaryDetector,
    ContentType,
    MarkdownBoundaryDetector,
    TextBoundaryDetector,
)


class TestCodeBoundaryDetector:
    """测试代码边界检测"""

    def setup_method(self):
        self.detector = CodeBoundaryDetector()

    def test_python_function_complete(self):
        """测试完整的 Python 函数"""
        code = """def calculate(n):
    if n <= 1:
        return n
    return calculate(n-1) + calculate(n-2)

# 其他代码"""

        result = self.detector.detect(code)
        assert result is not None
        assert result.boundary_type == BoundaryType.FUNCTION
        assert "return calculate(n-1)" in result.complete_part
        assert "# 其他代码" in result.incomplete_part

    def test_python_function_incomplete(self):
        """测试不完整的 Python 函数（在函数中间截断）"""
        code = """def calculate(n):
    if n <= 1:
        return n
    ret"""

        result = self.detector.detect(code)
        assert result is not None
        # 应该回滚到上一个完整语句
        assert "return n" in result.complete_part
        assert "ret" in result.incomplete_part or len(result.incomplete_part) == 0

    def test_python_class(self):
        """测试 Python 类定义"""
        code = """class Calculator:
    def __init__(self):
        self.result = 0

    def add(self, x):
        self.result += x

# 不完整的内容"""

        result = self.detector.detect(code)
        assert result is not None
        assert result.boundary_type in (
            BoundaryType.CLASS,
            BoundaryType.FUNCTION,
            BoundaryType.CODE_BLOCK,
        )
        assert "class Calculator" in result.complete_part

    def test_brace_balance(self):
        """测试括号平衡"""
        code = """function test() {
    if (true) {
        console.log("test");
    }
}
incomplete"""

        result = self.detector.detect(code)
        assert result is not None
        assert "}" in result.complete_part
        assert "incomplete" in result.incomplete_part

    def test_statement_boundary(self):
        """测试语句边界"""
        code = """x = 1
y = 2
z = x + y
incomplete_var"""

        result = self.detector.detect(code)
        assert result is not None
        assert result.boundary_type == BoundaryType.STATEMENT
        assert "z = x + y" in result.complete_part
        assert "incomplete_var" in result.incomplete_part


class TestMarkdownBoundaryDetector:
    """测试 Markdown 边界检测"""

    def setup_method(self):
        self.detector = MarkdownBoundaryDetector()

    def test_markdown_code_block(self):
        """测试 Markdown 代码块"""
        text = """# 标题

```python
def foo():
    return 42
```

这是段落，不完整"""

        result = self.detector.detect(text)
        assert result is not None
        assert result.boundary_type == BoundaryType.MARKDOWN_CODE
        assert "```" in result.complete_part
        assert result.complete_part.count("```") == 2  # 开始和结束
        assert "这是段落" in result.incomplete_part

    def test_markdown_header(self):
        """测试 Markdown 标题"""
        text = """# 主标题

内容1

## 子标题

不完整的内容"""

        result = self.detector.detect(text)
        assert result is not None
        # 应该找到最后一个标题
        assert "## 子标题" in result.complete_part
        assert "不完整的内容" in result.incomplete_part

    def test_markdown_list(self):
        """测试 Markdown 列表"""
        text = """列表：

- 项目1
- 项目2
- 项目3

不完整"""

        result = self.detector.detect(text)
        assert result is not None
        assert "- 项目" in result.complete_part
        assert "不完整" in result.incomplete_part


class TestTextBoundaryDetector:
    """测试文本边界检测"""

    def setup_method(self):
        self.detector = TextBoundaryDetector()

    def test_paragraph_boundary(self):
        """测试段落边界"""
        text = """第一段内容。
这是第一段的第二句。

第二段内容。不完整"""

        result = self.detector.detect(text)
        assert result is not None
        # 段落边界优先，但如果找到句子边界也可以接受
        assert result.boundary_type in (BoundaryType.PARAGRAPH, BoundaryType.SENTENCE)
        assert "第一段" in result.complete_part or "第二段内容。" in result.complete_part
        assert "不完整" in result.incomplete_part

    def test_chinese_sentence_boundary(self):
        """测试中文句子边界"""
        text = "这是第一句。这是第二句。这是不完整"

        result = self.detector.detect(text)
        assert result is not None
        assert result.boundary_type == BoundaryType.SENTENCE
        assert "这是第二句。" in result.complete_part
        assert "这是不完整" in result.incomplete_part

    def test_english_sentence_boundary(self):
        """测试英文句子边界"""
        text = "This is first sentence. This is second sentence. This is incomp"

        result = self.detector.detect(text)
        assert result is not None
        assert result.boundary_type == BoundaryType.SENTENCE
        assert "second sentence." in result.complete_part
        assert "This is incomp" in result.incomplete_part

    def test_word_boundary_fallback(self):
        """测试词语边界（保底）"""
        text = "这是一段 没有句号 的文本 不完整"

        result = self.detector.detect(text)
        assert result is not None
        assert result.boundary_type == BoundaryType.WORD
        assert len(result.complete_part) > 0
        assert len(result.incomplete_part) > 0

    def test_no_boundary_very_short(self):
        """测试极短文本"""
        text = "short"

        result = self.detector.detect(text)
        # 极短文本可能返回 None（无有效边界）
        # 但主检测器会提供保底方案
        if result is not None:
            assert len(result.complete_part) >= 0


class TestBoundaryDetector:
    """测试主检测器"""

    def setup_method(self):
        self.detector = BoundaryDetector()

    def test_auto_detect_code(self):
        """测试自动检测代码类型"""
        code = """def test():
    return 42
incomplete"""

        result = self.detector.find_last_boundary(code, ContentType.AUTO)
        assert result is not None
        assert "def test" in result.complete_part
        assert "incomplete" in result.incomplete_part

    def test_auto_detect_markdown(self):
        """测试自动检测 Markdown 类型"""
        text = """# 标题

```python
code
```

不完整"""

        result = self.detector.find_last_boundary(text, ContentType.AUTO)
        assert result is not None
        assert "```" in result.complete_part

    def test_auto_detect_text(self):
        """测试自动检测纯文本类型"""
        text = "这是一段普通文本。这是第二句。不完整"

        result = self.detector.find_last_boundary(text, ContentType.AUTO)
        assert result is not None
        assert "第二句。" in result.complete_part

    def test_explicit_code_type(self):
        """测试显式指定代码类型"""
        code = "def foo():\n    return 1\ninc"

        result = self.detector.find_last_boundary(code, ContentType.CODE)
        assert result is not None
        assert "return 1" in result.complete_part

    def test_explicit_markdown_type(self):
        """测试显式指定 Markdown 类型"""
        text = "# Title\n\nContent\n\ninc"

        result = self.detector.find_last_boundary(text, ContentType.MARKDOWN)
        assert result is not None

    def test_explicit_text_type(self):
        """测试显式指定文本类型"""
        text = "句子一。句子二。不完整"

        result = self.detector.find_last_boundary(text, ContentType.TEXT)
        assert result is not None
        assert "句子二。" in result.complete_part

    def test_always_returns_result(self):
        """测试总是返回结果（即使是保底方案）"""
        # 完全没有边界的文本
        text = "nospacesorpunctuation"

        result = self.detector.find_last_boundary(text)
        assert result is not None
        assert len(result.complete_part) > 0 or len(result.incomplete_part) > 0


class TestBoundaryResult:
    """测试 BoundaryResult 数据结构"""

    def test_basic_properties(self):
        """测试基本属性"""
        result = BoundaryResult(
            complete_part="完整部分",
            incomplete_part="不完整",
            boundary_type=BoundaryType.SENTENCE,
            boundary_position=12,
            confidence=0.9,
        )

        assert len(result) == 4  # "完整部分" 4个字符
        assert result.is_valid(min_length=3)
        assert not result.is_valid(min_length=10)

    def test_rollback_ratio(self):
        """测试回滚比例"""
        result = BoundaryResult(
            complete_part="x" * 80,
            incomplete_part="x" * 20,
            boundary_type=BoundaryType.WORD,
            boundary_position=80,
            confidence=0.5,
        )

        ratio = result.rollback_ratio()
        assert 0.19 <= ratio <= 0.21  # 20/100 = 0.2

    def test_to_dict(self):
        """测试转换为字典"""
        result = BoundaryResult(
            complete_part="complete",
            incomplete_part="incomplete",
            boundary_type=BoundaryType.PARAGRAPH,
            boundary_position=8,
            confidence=0.85,
            metadata={"test": "value"},
        )

        data = result.to_dict()
        assert data["complete_length"] == 8
        assert data["incomplete_length"] == 10
        assert data["boundary_type"] == "paragraph"
        assert data["confidence"] == 0.85
        assert "rollback_ratio" in data
        assert data["metadata"]["test"] == "value"


class TestRealWorldScenarios:
    """测试真实世界场景"""

    def setup_method(self):
        self.detector = BoundaryDetector()

    def test_long_python_code(self):
        """测试长 Python 代码"""
        code = """def fibonacci(n):
    '''计算斐波那契数列'''
    if n <= 1:
        return n

    # 递归计算
    return fibonacci(n-1) + fibonacci(n-2)

def factorial(n):
    '''计算阶乘'''
    if n <= 1:
        return 1
    return n * facto"""  # 不完整

        result = self.detector.find_last_boundary(code, ContentType.CODE)
        assert result is not None
        # 应该保留完整的 fibonacci 函数
        assert "fibonacci(n-1)" in result.complete_part
        # 不完整的 factorial 在 incomplete 部分
        assert "facto" in result.incomplete_part or "factorial" in result.incomplete_part

    def test_markdown_document(self):
        """测试 Markdown 文档"""
        text = """# 使用指南

## 安装

```bash
pip install package
```

## 使用方法

创建实例：

```python
from package import Class
obj = Cl"""  # 不完整

        result = self.detector.find_last_boundary(text, ContentType.MARKDOWN)
        assert result is not None
        # 应该包含完整的代码块
        assert result.complete_part.count("```") % 2 == 0  # 偶数个

    def test_mixed_content(self):
        """测试混合内容"""
        text = """这是一段说明文字。

```python
def example():
    return "hello"
```

后续说明。这里不完"""

        result = self.detector.find_last_boundary(text)
        assert result is not None
        # 应该检测为 Markdown 并保留完整代码块
        if "```" in result.complete_part:
            assert result.complete_part.count("```") == 2

    def test_chinese_article(self):
        """测试中文文章"""
        text = """这是第一段内容。它包含多个句子。这是第三句话。

这是第二段的开始。第二段还在继续。这里不完整，没有"""

        result = self.detector.find_last_boundary(text, ContentType.TEXT)
        assert result is not None
        # 应该保留到完整的段落或句子
        assert "。" in result.complete_part  # 至少有一个句号
        assert len(result.complete_part) > 20

    def test_english_article(self):
        """测试英文文章"""
        text = """This is the first paragraph. It contains multiple sentences.
This is the third sentence.

This is the second paragraph. It continues here. This is incomp"""

        result = self.detector.find_last_boundary(text, ContentType.TEXT)
        assert result is not None
        assert "sentence" in result.complete_part
        assert "incomp" in result.incomplete_part
