"""自定义命令启动链路回归测试。"""

from __future__ import annotations


class _EmptyShortcutManager:
    def list_all(self):
        return []


class _EmptySkillManager:
    load_errors: list[str] = []

    def list_all(self):
        return []


def test_load_custom_commands_uses_command_registry_boundary(monkeypatch):
    """命令实现拆包后，启动加载器仍应能注册动态命令。"""
    from xenon.repl import shortcut_manager, skill_manager
    from xenon.repl.repl import REPL

    monkeypatch.setattr(shortcut_manager, "ShortcutManager", _EmptyShortcutManager)
    monkeypatch.setattr(skill_manager, "SkillManager", _EmptySkillManager)

    repl = REPL.__new__(REPL)
    repl._load_custom_commands()
