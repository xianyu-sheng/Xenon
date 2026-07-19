"""CodeEditor 单元测试。"""

from pathlib import Path

import pytest

from xenon.repl.code_editor import CodeEditor


class TestCodeEditor:
    def test_read_file(self, tmp_path):
        """读取文件并返回带行号的内容。"""
        f = tmp_path / "test.py"
        f.write_text("line 1\nline 2\nline 3")

        content, count = CodeEditor.read_file(f)
        assert count == 3
        assert "1 | line 1" in content
        assert "2 | line 2" in content
        assert "3 | line 3" in content

    def test_read_file_not_found(self, tmp_path):
        """文件不存在时抛出异常。"""
        with pytest.raises(FileNotFoundError):
            CodeEditor.read_file(tmp_path / "nonexistent.py")

    def test_read_raw(self, tmp_path):
        """读取原始内容。"""
        f = tmp_path / "test.py"
        f.write_text("hello\nworld")
        content = CodeEditor.read_raw(f)
        assert content == "hello\nworld"

    def test_apply_edit(self, tmp_path):
        """精确替换文本。"""
        f = tmp_path / "test.py"
        f.write_text("def foo():\n    return 1\n")

        result = CodeEditor.apply_edit(f, "return 1", "return 42", confirm=False)
        assert "✅" in result
        assert f.read_text() == "def foo():\n    return 42\n"

    def test_apply_edit_not_found(self, tmp_path):
        """old_text 不存在时返回错误。"""
        f = tmp_path / "test.py"
        f.write_text("hello world")

        result = CodeEditor.apply_edit(f, "nonexistent", "new", confirm=False)
        assert "❌" in result
        assert "未找到" in result

    def test_apply_edit_multiple_matches(self, tmp_path):
        """多处匹配时返回错误。"""
        f = tmp_path / "test.py"
        f.write_text("x = 1\ny = 1\nz = 1")

        result = CodeEditor.apply_edit(f, "1", "2", confirm=False)
        assert "⚠️" in result
        assert "3 处匹配" in result

    def test_apply_edit_file_not_found(self, tmp_path):
        """文件不存在时返回错误。"""
        result = CodeEditor.apply_edit(tmp_path / "no.py", "old", "new", confirm=False)
        assert "❌" in result

    def test_generate_diff(self):
        """生成 unified diff。"""
        diff = CodeEditor.generate_diff("hello\n", "world\n", "test.txt")
        assert "--- a/test.txt" in diff
        assert "+++ b/test.txt" in diff
        assert "-hello" in diff
        assert "+world" in diff

    def test_extract_code_with_ext(self):
        """从 LLM 输出中提取带语言标记的代码块。"""
        response = 'Here is the code:\n```python\nprint("hello")\n```\nDone.'
        code = CodeEditor._extract_code(response, "python")
        assert code == 'print("hello")'

    def test_extract_code_without_ext(self):
        """从 LLM 输出中提取无语言标记的代码块。"""
        response = '```\nprint("hello")\n```'
        code = CodeEditor._extract_code(response, "py")
        assert code == 'print("hello")'

    def test_extract_code_plain(self):
        """从纯代码文本中提取。"""
        response = 'print("hello")\n'
        code = CodeEditor._extract_code(response, "python")
        assert code == 'print("hello")'
