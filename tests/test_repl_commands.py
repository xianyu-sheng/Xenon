"""Xenon REPL 斜杠命令测试

测试所有关键的斜杠命令功能
注意：Xenon 使用单斜杠 / 作为命令前缀
"""
import pytest


class TestSlashCommands:
    """测试所有关键斜杠命令"""

    def test_commands_registered(self):
        """测试命令是否正确注册"""
        from xenon.repl.commands import COMMANDS

        # 验证关键命令已注册
        essential_commands = [
            "/help", "/mode", "/models", "/set_model", "/tools",
            "/status", "/clear", "/save", "/resume", "/setup",
            "/exit", "/quit"
        ]

        for cmd in essential_commands:
            assert cmd in COMMANDS, f"命令 {cmd} 未注册"

    def test_all_commands_registered(self):
        """测试所有命令都已注册"""
        from xenon.repl.commands import COMMANDS
        # Xenon 有约 49 个命令
        assert len(COMMANDS) >= 45, f"应该有至少 45 个命令，实际有 {len(COMMANDS)} 个"

    def test_command_categories(self):
        """测试命令分类完整性"""
        from xenon.repl.commands import COMMANDS

        # 模型相关
        model_commands = ["/mode", "/models", "/set_model"]
        for cmd in model_commands:
            assert cmd in COMMANDS, f"模型命令 {cmd} 缺失"

        # 会话相关
        session_commands = ["/save", "/resume", "/clear", "/history"]
        for cmd in session_commands:
            assert cmd in COMMANDS, f"会话命令 {cmd} 缺失"

        # 工具相关
        tool_commands = ["/tools", "/setup"]
        for cmd in tool_commands:
            assert cmd in COMMANDS, f"工具命令 {cmd} 缺失"

        # 系统相关
        system_commands = ["/help", "/status", "/exit", "/quit"]
        for cmd in system_commands:
            assert cmd in COMMANDS, f"系统命令 {cmd} 缺失"


class TestCommandList:
    """测试完整命令列表"""

    def test_list_all_commands(self):
        """列出所有命令用于文档"""
        from xenon.repl.commands import COMMANDS

        print("\n" + "="*70)
        print("Xenon 所有斜杠命令列表 (单斜杠 /)")
        print("="*70)

        categories = {
            "模型管理": ["/mode", "/models", "/set_model", "/set_profile",
                       "/set_role", "/pool", "/provider", "/import_models",
                       "/reload_models", "/remove_model"],
            "会话管理": ["/save", "/resume", "/clear", "/history", "/sessions",
                       "/load", "/compact", "/undo"],
            "工具管理": ["/tools", "/setup", "/mcp", "/library"],
            "技能管理": ["/skill", "/skill-discover", "/skill-install", "/shortcut"],
            "系统信息": ["/help", "/status", "/cost", "/cache", "/fix-cache", "/context"],
            "运行控制": ["/run", "/code", "/ask", "/sub-agent"],
            "显示选项": ["/optimize", "/stream", "/thinking", "/verbose", "/vision"],
            "配置管理": ["/config", "/project", "/permissions", "/memory"],
            "退出命令": ["/exit", "/quit", "/bye"],
        }

        found_count = 0
        for category, commands in categories.items():
            print(f"\n{category}:")
            for cmd in commands:
                if cmd in COMMANDS:
                    desc = COMMANDS[cmd].get('description', '')
                    print(f"  ✓ {cmd:20} - {desc[:40]}")
                    found_count += 1
                else:
                    print(f"  ✗ {cmd:20} (未找到)")

        # 找出未分类的命令
        all_categorized = set()
        for cmds in categories.values():
            all_categorized.update(cmds)

        uncategorized = set(COMMANDS.keys()) - all_categorized
        if uncategorized:
            print("\n未分类命令:")
            for cmd in sorted(uncategorized):
                desc = COMMANDS[cmd].get('description', '')
                print(f"  ? {cmd:20} - {desc[:40]}")

        print("\n" + "="*70)
        print(f"总计: {len(COMMANDS)} 个注册命令")
        print(f"已分类: {found_count} 个")
        print(f"未分类: {len(uncategorized)} 个")
        print("="*70)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
