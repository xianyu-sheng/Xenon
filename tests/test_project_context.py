"""ProjectContext 单元测试。"""

import os
import tempfile
from pathlib import Path

import pytest

from omniagent.repl.project_context import ProjectContext


class TestProjectContext:
    def test_detect_python_project(self, tmp_path):
        """检测 Python 项目。"""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'")
        pc = ProjectContext()
        found = pc.detect(tmp_path)
        assert found is True
        assert pc.project_type == "python"
        assert pc.root == tmp_path

    def test_detect_node_project(self, tmp_path):
        """检测 Node.js 项目。"""
        (tmp_path / "package.json").write_text('{"name": "test"}')
        pc = ProjectContext()
        found = pc.detect(tmp_path)
        assert found is True
        assert pc.project_type == "node"

    def test_detect_rust_project(self, tmp_path):
        """检测 Rust 项目。"""
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "test"')
        pc = ProjectContext()
        found = pc.detect(tmp_path)
        assert found is True
        assert pc.project_type == "rust"

    def test_detect_git_project(self, tmp_path):
        """通过 .git 目录检测项目。"""
        (tmp_path / ".git").mkdir()
        pc = ProjectContext()
        found = pc.detect(tmp_path)
        assert found is True

    def test_detect_unknown_project(self, tmp_path):
        """无标记文件时返回 unknown。"""
        pc = ProjectContext()
        found = pc.detect(tmp_path)
        assert pc.project_type == "unknown"

    def test_load_rules(self, tmp_path):
        """加载 .omniagent/rules.md。"""
        rules_dir = tmp_path / ".omniagent"
        rules_dir.mkdir()
        (rules_dir / "rules.md").write_text("使用 Python 3.12\n遵循 PEP 8")
        (tmp_path / "pyproject.toml").write_text("")

        pc = ProjectContext()
        pc.detect(tmp_path)
        assert "Python 3.12" in pc.rules
        assert "PEP 8" in pc.rules

    def test_file_tree(self, tmp_path):
        """构建文件树。"""
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hello')")
        (tmp_path / "README.md").write_text("# Test")

        pc = ProjectContext()
        pc.detect(tmp_path)
        assert "src/" in pc.file_tree
        assert "main.py" in pc.file_tree
        assert "README.md" in pc.file_tree

    def test_file_tree_excludes_dirs(self, tmp_path):
        """文件树排除 node_modules 等目录。"""
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "pkg.js").write_text("")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "mod.pyc").write_text("")

        pc = ProjectContext()
        pc.detect(tmp_path)
        assert "node_modules" not in pc.file_tree
        assert "__pycache__" not in pc.file_tree

    def test_key_files(self, tmp_path):
        """加载关键配置文件。"""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"')
        (tmp_path / "README.md").write_text("# My Project")

        pc = ProjectContext()
        pc.detect(tmp_path)
        assert "pyproject.toml" in pc.key_files
        assert "README.md" in pc.key_files
        assert "test" in pc.key_files["pyproject.toml"]

    def test_format_for_context(self, tmp_path):
        """格式化上下文输出。"""
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "main.py").write_text("x = 1")

        pc = ProjectContext()
        pc.detect(tmp_path)
        ctx = pc.format_for_context()
        assert "Python" in ctx
        assert "文件结构" in ctx

    def test_get_summary(self, tmp_path):
        """返回项目摘要。"""
        (tmp_path / "pyproject.toml").write_text("")
        pc = ProjectContext()
        pc.detect(tmp_path)
        summary = pc.get_summary()
        assert "Python" in summary

    def test_refresh(self, tmp_path):
        """刷新项目上下文。"""
        (tmp_path / "pyproject.toml").write_text("")
        pc = ProjectContext()
        pc.detect(tmp_path)
        assert pc.rules == ""

        # 添加规则文件后刷新
        rules_dir = tmp_path / ".omniagent"
        rules_dir.mkdir()
        (rules_dir / "rules.md").write_text("新规则")
        pc.refresh()
        assert "新规则" in pc.rules

    def test_rules_length_limit(self, tmp_path):
        """规则文件超过 3000 字符时截断。"""
        rules_dir = tmp_path / ".omniagent"
        rules_dir.mkdir()
        (rules_dir / "rules.md").write_text("x" * 5000)
        (tmp_path / "pyproject.toml").write_text("")

        pc = ProjectContext()
        pc.detect(tmp_path)
        assert len(pc.rules) <= 3000
