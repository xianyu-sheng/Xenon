"""
Shortcut Manager 测试。
"""

from __future__ import annotations

import tempfile
from pathlib import Path


from xenon.repl.shortcut_manager import ShortcutManager


class TestShortcutManager:
    """测试快捷指令管理器。"""

    def test_create_and_list(self):
        """测试创建和列出快捷指令。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ShortcutManager(Path(tmpdir) / "shortcuts.yaml")
            manager.create("hello", "打招呼", ["echo hello world"])

            assert len(manager.shortcuts) == 1
            assert "hello" in manager.shortcuts
            assert manager.shortcuts["hello"].description == "打招呼"

    def test_remove(self):
        """测试删除快捷指令。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ShortcutManager(Path(tmpdir) / "shortcuts.yaml")
            manager.create("test", "测试", ["echo test"])

            assert manager.remove("test") is True
            assert len(manager.shortcuts) == 0

    def test_remove_nonexistent(self):
        """测试删除不存在的快捷指令。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ShortcutManager(Path(tmpdir) / "shortcuts.yaml")
            assert manager.remove("nonexistent") is False

    def test_persistence(self):
        """测试持久化。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "shortcuts.yaml"
            manager1 = ShortcutManager(path)
            manager1.create("persist", "持久化测试", ["echo ok"])

            manager2 = ShortcutManager(path)
            assert "persist" in manager2.shortcuts
            assert manager2.shortcuts["persist"].description == "持久化测试"

    def test_execute_echo(self):
        """测试执行 echo 命令。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ShortcutManager(Path(tmpdir) / "shortcuts.yaml")
            manager.create("hi", "打招呼", ["echo hello"])

            result = manager.execute("hi")
            assert "hello" in result

    def test_execute_with_params(self):
        """测试带参数执行。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ShortcutManager(Path(tmpdir) / "shortcuts.yaml")
            manager.create(
                "greet", "问候", ["echo hello {name}"],
                params=[{"name": "name", "default": "world"}],
            )

            result = manager.execute("greet", "Alice")
            assert "hello Alice" in result

    def test_execute_default_params(self):
        """测试默认参数。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ShortcutManager(Path(tmpdir) / "shortcuts.yaml")
            manager.create(
                "greet", "问候", ["echo hello {name}"],
                params=[{"name": "name", "default": "world"}],
            )

            result = manager.execute("greet")
            assert "hello world" in result

    def test_execute_nonexistent(self):
        """测试执行不存在的快捷指令。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ShortcutManager(Path(tmpdir) / "shortcuts.yaml")
            result = manager.execute("nonexistent")
            assert "不存在" in result

    def test_name_cleanup(self):
        """测试名称清理。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ShortcutManager(Path(tmpdir) / "shortcuts.yaml")
            manager.create("/My Command", "测试", ["echo ok"])

            assert "my_command" in manager.shortcuts

    def test_multiple_steps(self):
        """测试多步骤执行。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ShortcutManager(Path(tmpdir) / "shortcuts.yaml")
            manager.create("multi", "多步骤", ["echo step1", "echo step2"])

            result = manager.execute("multi")
            assert "step1" in result
            assert "step2" in result

    def test_list_all(self):
        """测试列出所有。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ShortcutManager(Path(tmpdir) / "shortcuts.yaml")
            manager.create("a", "A", ["echo a"])
            manager.create("b", "B", ["echo b"])

            all_sc = manager.list_all()
            assert len(all_sc) == 2
