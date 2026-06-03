"""
Skill Manager 测试。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from omniagent.repl.skill_manager import SkillManager, Skill, SkillStep


class TestSkillManager:
    """测试技能管理器。"""

    def test_create_and_list(self):
        """测试创建和列出技能。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            manager.create("test", "测试技能", [
                {"type": "echo", "prompt": "hello"},
            ])

            assert len(manager.skills) == 1
            assert "test" in manager.skills

    def test_remove(self):
        """测试删除技能。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            manager.create("test", "测试", [{"type": "echo", "prompt": "ok"}])

            assert manager.remove("test") is True
            assert len(manager.skills) == 0

    def test_remove_nonexistent(self):
        """测试删除不存在的技能。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            assert manager.remove("nonexistent") is False

    def test_persistence(self):
        """测试持久化。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            manager1 = SkillManager(skills_dir)
            manager1.create("persist", "持久化", [{"type": "echo", "prompt": "ok"}])

            manager2 = SkillManager(skills_dir)
            assert "persist" in manager2.skills
            assert manager2.skills["persist"].description == "持久化"

    def test_execute_echo(self):
        """测试执行 echo 步骤。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            manager.create("hello", "打招呼", [
                {"type": "echo", "prompt": "hello world"},
            ])

            result = manager.execute("hello", "")
            assert "hello world" in result

    def test_execute_command(self):
        """测试执行 command 步骤。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            manager.create("cmd", "命令测试", [
                {"type": "command", "action": "echo test"},
            ])

            result = manager.execute("cmd", "")
            assert "test" in result

    def test_execute_with_params(self):
        """测试带参数执行。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            manager.create("greet", "问候", [
                {"type": "echo", "prompt": "hello {name}"},
            ], params=[{"name": "name", "default": "world"}])

            result = manager.execute("greet", "Alice")
            assert "hello Alice" in result

    def test_execute_nonexistent(self):
        """测试执行不存在的技能。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            result = manager.execute("nonexistent", "")
            assert "不存在" in result

    def test_multiple_steps(self):
        """测试多步骤执行。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            manager.create("multi", "多步骤", [
                {"type": "echo", "prompt": "step1", "output_var": "r1"},
                {"type": "echo", "prompt": "step2"},
            ])

            result = manager.execute("multi", "")
            assert "step1" in result
            assert "step2" in result

    def test_output_var(self):
        """测试输出变量。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            manager.create("vars", "变量测试", [
                {"type": "echo", "prompt": "hello", "output_var": "greeting"},
                {"type": "echo", "prompt": "result: {greeting}"},
            ])

            result = manager.execute("vars", "")
            assert "result: hello" in result

    def test_write_and_read_file(self):
        """测试文件读写步骤。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            test_file = Path(tmpdir) / "test.txt"

            manager.create("filer", "文件操作", [
                {"type": "write_file", "file_path": str(test_file), "content": "file content"},
                {"type": "read_file", "file_path": str(test_file), "output_var": "data"},
                {"type": "echo", "prompt": "read: {data}"},
            ])

            result = manager.execute("filer", "")
            assert "file content" in result

    def test_list_all(self):
        """测试列出所有。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            manager.create("a", "A", [{"type": "echo", "prompt": "a"}])
            manager.create("b", "B", [{"type": "echo", "prompt": "b"}])

            all_skills = manager.list_all()
            assert len(all_skills) == 2

    def test_skill_step_dataclass(self):
        """测试 SkillStep 数据类。"""
        step = SkillStep(type="llm", prompt="test", output_var="out")
        assert step.type == "llm"
        assert step.prompt == "test"
        assert step.output_var == "out"
