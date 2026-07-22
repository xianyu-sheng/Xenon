"""ProjectContext 单元测试。"""

from xenon.repl.project_context import ProjectContext


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
        pc.detect(tmp_path)
        assert pc.project_type == "unknown"

    def test_load_rules(self, tmp_path):
        """加载 .xenon/rules.md。"""
        rules_dir = tmp_path / ".xenon"
        rules_dir.mkdir()
        (rules_dir / "rules.md").write_text("使用 Python 3.12\n遵循 PEP 8", encoding="utf-8")
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
        rules_dir = tmp_path / ".xenon"
        rules_dir.mkdir()
        (rules_dir / "rules.md").write_text("新规则", encoding="utf-8")
        pc.refresh()
        assert "新规则" in pc.rules

    def test_rules_length_limit(self, tmp_path):
        """规则文件超过 3000 字符时截断。"""
        rules_dir = tmp_path / ".xenon"
        rules_dir.mkdir()
        (rules_dir / "rules.md").write_text("x" * 5000)
        (tmp_path / "pyproject.toml").write_text("")

        pc = ProjectContext()
        pc.detect(tmp_path)
        assert len(pc.rules) <= 3000

    def test_layered_xenon_rules_and_agents_fallback(self, tmp_path):
        global_root = tmp_path / "global"
        global_root.mkdir()
        (global_root / "XENON.md").write_text("全局规则", encoding="utf-8")
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "AGENTS.md").write_text("后备项目规则", encoding="utf-8")
        (tmp_path / "XENON.local.md").write_text("本地覆盖规则", encoding="utf-8")

        pc = ProjectContext(global_config_root=global_root)
        pc.detect(tmp_path)

        assert pc.rules.index("全局规则") < pc.rules.index("后备项目规则")
        assert pc.rules.index("后备项目规则") < pc.rules.index("本地覆盖规则")

        (tmp_path / "XENON.md").write_text("正式项目规则", encoding="utf-8")
        pc.refresh()
        assert "正式项目规则" in pc.rules
        assert "后备项目规则" not in pc.rules

    def test_nested_rules_override_root_and_imports_are_root_bounded(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "shared.md").write_text("安全导入内容", encoding="utf-8")
        (tmp_path / "XENON.md").write_text("根规则\n@shared.md", encoding="utf-8")
        nested = tmp_path / "src" / "feature"
        nested.mkdir(parents=True)
        (nested / "XENON.md").write_text("最近目录规则", encoding="utf-8")
        outside = tmp_path.parent / "outside-memory-rule.md"
        outside.write_text("不应导入的内容", encoding="utf-8")
        (nested / "XENON.local.md").write_text(
            "嵌套本地规则\n@../../../outside-memory-rule.md",
            encoding="utf-8",
        )

        pc = ProjectContext(global_config_root=tmp_path / "missing-global")
        pc.detect(nested)

        assert pc.rules.index("根规则") < pc.rules.index("最近目录规则")
        assert pc.rules.index("最近目录规则") < pc.rules.index("嵌套本地规则")
        assert "安全导入内容" in pc.rules
        assert "不应导入的内容" not in pc.rules

    def test_instruction_import_cycle_is_ignored(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "XENON.md").write_text("主规则\n@a.md", encoding="utf-8")
        (tmp_path / "a.md").write_text("子规则\n@XENON.md", encoding="utf-8")

        pc = ProjectContext(global_config_root=tmp_path / "missing-global")
        pc.detect(tmp_path)

        assert "主规则" in pc.rules
        assert "子规则" in pc.rules
        assert len(pc.rules) < 1000
