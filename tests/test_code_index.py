"""
Code Index 和 AST Analyzer 测试。
"""

from __future__ import annotations



from xenon.utils.code_index import CodeIndex
from xenon.utils.ast_analyzer import ASTAnalyzer
from xenon.utils.refactor import RefactorEngine


class TestCodeIndex:
    """代码索引测试。"""

    def test_index_python_functions(self, tmp_path):
        """索引 Python 函数。"""
        code = '''
def hello():
    pass

def add(a, b):
    return a + b

class Foo:
    def bar(self):
        pass
'''
        (tmp_path / "test.py").write_text(code)
        index = CodeIndex(tmp_path)
        count = index.build()
        assert count == 1

        # 搜索函数
        results = index.search("hello")
        assert any(r.kind == "function" for r in results)

        results = index.search("add")
        assert any(r.kind == "function" and r.signature == "a, b" for r in results)

        # 搜索方法
        results = index.search("bar")
        assert any(r.kind == "method" and r.parent == "Foo" for r in results)

        # 搜索类
        results = index.search("Foo")
        assert any(r.kind == "class" for r in results)

    def test_index_python_imports(self, tmp_path):
        """索引 Python 导入。"""
        code = '''
import os
from pathlib import Path
from typing import Any, Dict
'''
        (tmp_path / "test.py").write_text(code)
        index = CodeIndex(tmp_path)
        index.build()

        imports = index.get_imports(str(tmp_path / "test.py"))
        assert len(imports) >= 3  # os, Path, Any, Dict

    def test_index_multiple_files(self, tmp_path):
        """索引多个文件。"""
        (tmp_path / "a.py").write_text("def func_a(): pass")
        (tmp_path / "b.py").write_text("def func_b(): pass")
        (tmp_path / "c.js").write_text("function func_c() {}")

        index = CodeIndex(tmp_path)
        count = index.build()
        assert count == 3

        results = index.search("func_")
        names = {r.name for r in results}
        assert "func_a" in names
        assert "func_b" in names
        assert "func_c" in names

    def test_find_definition(self, tmp_path):
        """精确查找定义。"""
        code = '''
def target_func():
    pass
'''
        (tmp_path / "test.py").write_text(code)
        index = CodeIndex(tmp_path)
        index.build()

        defs = index.find_definition("target_func")
        assert len(defs) == 1
        assert defs[0].kind == "function"

    def test_find_references(self, tmp_path):
        """查找引用。"""
        (tmp_path / "a.py").write_text("def my_func(): pass\nmy_func()")
        (tmp_path / "b.py").write_text("from a import my_func\nmy_func()")

        index = CodeIndex(tmp_path)
        index.build()

        refs = index.find_references("my_func")
        assert len(refs) >= 3  # def + 2 calls

    def test_stats(self, tmp_path):
        """索引统计。"""
        (tmp_path / "a.py").write_text("def f(): pass\nclass C: pass")
        index = CodeIndex(tmp_path)
        index.build()

        stats = index.stats()
        assert stats["files"] == 1
        assert stats["symbols"] >= 2
        assert "function" in stats["by_kind"]
        assert "class" in stats["by_kind"]

    def test_exclude_dirs(self, tmp_path):
        """排除目录。"""
        (tmp_path / "good.py").write_text("def good(): pass")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "cached.py").write_text("def cached(): pass")

        index = CodeIndex(tmp_path)
        index.build()

        results = index.search("good")
        assert len(results) == 1
        results = index.search("cached")
        assert len(results) == 0

    def test_empty_file(self, tmp_path):
        """空文件不崩溃。"""
        (tmp_path / "empty.py").write_text("")
        index = CodeIndex(tmp_path)
        count = index.build()
        assert count == 1

    def test_syntax_error_file(self, tmp_path):
        """语法错误文件降级处理。"""
        (tmp_path / "bad.py").write_text("def foo(:\n  pass")
        index = CodeIndex(tmp_path)
        count = index.build()
        assert count == 1  # 不崩溃


class TestASTAnalyzer:
    """AST 分析器测试。"""

    def test_analyze_functions(self):
        """分析函数。"""
        code = '''
def hello(name: str) -> str:
    """Say hello."""
    return f"Hello {name}"

async def fetch(url):
    pass
'''
        analyzer = ASTAnalyzer()
        result = analyzer.analyze_code(code)

        assert result.syntax_valid
        assert len(result.functions) == 2
        assert result.functions[0].name == "hello"
        assert result.functions[0].return_annotation == "str"
        assert result.functions[0].docstring == "Say hello."
        assert result.functions[1].is_async

    def test_analyze_classes(self):
        """分析类。"""
        code = '''
class Animal:
    """Base animal."""
    def speak(self):
        pass

class Dog(Animal):
    def bark(self):
        pass
'''
        analyzer = ASTAnalyzer()
        result = analyzer.analyze_code(code)

        assert len(result.classes) == 2
        assert result.classes[0].name == "Animal"
        assert result.classes[1].bases == ["Animal"]
        assert len(result.classes[1].methods) == 1

    def test_check_syntax_valid(self):
        """语法检查 — 有效代码。"""
        analyzer = ASTAnalyzer()
        errors = analyzer.check_syntax("def foo(): pass")
        assert errors == []

    def test_check_syntax_invalid(self):
        """语法检查 — 无效代码。"""
        analyzer = ASTAnalyzer()
        errors = analyzer.check_syntax("def foo(:\n  pass")
        assert len(errors) > 0
        assert "行" in errors[0]

    def test_extract_signatures(self):
        """提取函数签名。"""
        code = '''
def add(a: int, b: int) -> int:
    return a + b

def greet(name, greeting="Hello"):
    pass
'''
        analyzer = ASTAnalyzer()
        sigs = analyzer.extract_signatures(code)

        assert len(sigs) == 2
        assert sigs[0]["name"] == "add"
        assert "a: int" in sigs[0]["args"]
        assert sigs[0]["return"] == "int"

    def test_complexity(self):
        """复杂度计算。"""
        code = '''
def complex_func(x):
    if x > 0:
        for i in range(x):
            if i % 2 == 0:
                print(i)
    elif x < 0:
        while x < 0:
            x += 1
    return x
'''
        analyzer = ASTAnalyzer()
        result = analyzer.analyze_code(code)
        assert result.complexity > 1

    def test_unused_imports(self):
        """未使用导入检测。"""
        code = '''
import os
import sys
from pathlib import Path

p = Path(".")
'''
        analyzer = ASTAnalyzer()
        result = analyzer.analyze_code(code)
        assert "os" in result.unused_imports
        assert "sys" in result.unused_imports

    def test_summary(self):
        """摘要格式。"""
        code = '''
def f(): pass
class C: pass
'''
        analyzer = ASTAnalyzer()
        result = analyzer.analyze_code(code)
        summary = result.summary()
        assert "函数: 1" in summary
        assert "类: 1" in summary


class TestRefactorEngine:
    """重构引擎测试。"""

    def test_rename_symbol(self, tmp_path):
        """跨文件重命名。"""
        (tmp_path / "a.py").write_text("def old_func(): pass\nold_func()")
        (tmp_path / "b.py").write_text("from a import old_func\nold_func()")

        engine = RefactorEngine(tmp_path)
        engine.build_index()

        result = engine.rename_symbol("old_func", "new_func")
        assert result["success"]
        assert result["files_modified"] >= 2

        # 验证文件内容
        content_a = (tmp_path / "a.py").read_text()
        assert "new_func" in content_a
        assert "old_func" not in content_a

    def test_rename_dry_run(self, tmp_path):
        """dry_run 不修改文件。"""
        (tmp_path / "a.py").write_text("def old(): pass\nold()")

        engine = RefactorEngine(tmp_path)
        engine.build_index()

        result = engine.rename_symbol("old", "new", dry_run=True)
        assert result["success"]

        # 文件未被修改
        assert "old" in (tmp_path / "a.py").read_text()

    def test_clean_unused_imports(self, tmp_path):
        """清理未使用导入。"""
        (tmp_path / "test.py").write_text("import os\nimport sys\nx = 1")

        engine = RefactorEngine(tmp_path)
        result = engine.clean_unused_imports(tmp_path / "test.py")
        assert result["success"]

    def test_analyze_for_refactor(self, tmp_path):
        """分析重构建议。"""
        code = '''
import os

def very_long_function():
    """A very long function."""
    x = 1
    if x > 0:
        for i in range(10):
            if i % 2 == 0:
                for j in range(5):
                    if j > 2:
                        print(i + j)
    return x
'''
        (tmp_path / "test.py").write_text(code)

        engine = RefactorEngine(tmp_path)
        result = engine.analyze_for_refactor(tmp_path / "test.py")
        assert "suggestions" in result
        assert "summary" in result
