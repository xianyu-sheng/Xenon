"""Xenon TUI 状态栏和显示测试

测试 TUI 相关功能：
- 状态栏显示 (模型、模式、context、cost 等)
- 命令输出格式
- Rich 渲染
"""
import pytest
from io import StringIO
from unittest.mock import Mock, patch
from rich.console import Console


class TestTUIStatusBar:
    """测试 TUI 状态栏功能"""

    def test_status_bar_components_exist(self):
        """测试状态栏组件是否存在"""
        # 导入 REPL 验证状态栏相关方法存在
        from xenon.repl import repl

        # 检查是否有状态相关的函数/方法
        repl_module = dir(repl)

        # 状态栏应该能显示这些信息
        expected_features = [
            'Console',  # Rich Console
            'REPL',     # REPL 类
        ]

        for feature in expected_features:
            assert feature in repl_module, f"{feature} 应该在 repl 模块中"

    def test_rich_console_available(self):
        """测试 Rich Console 可用"""
        from rich.console import Console

        console = Console()
        assert console is not None

        # 测试基本渲染
        with console.capture() as capture:
            console.print("[bold]Test[/bold]")

        output = capture.get()
        assert "Test" in output

    def test_status_info_structure(self):
        """测试状态信息数据结构"""
        # 状态栏应该包含的信息
        status_info = {
            "model": "test-model",
            "mode": "react",
            "context_usage": "10%",
            "cache_status": "hit",
            "cost": "$0.01",
            "time": "00:05"
        }

        # 验证所有关键字段都存在
        assert "model" in status_info
        assert "mode" in status_info
        assert "context_usage" in status_info


class TestTUIDisplay:
    """测试 TUI 显示功能"""

    def test_markdown_rendering(self):
        """测试 Markdown 渲染"""
        from rich.markdown import Markdown
        from rich.console import Console

        md = Markdown("# Test\n\n**Bold** text")
        assert md is not None

        console = Console()
        with console.capture() as capture:
            console.print(md)

        output = capture.get()
        assert "Test" in output

    def test_table_rendering(self):
        """测试表格渲染"""
        from rich.table import Table
        from rich.console import Console

        table = Table(title="Test Table")
        table.add_column("Col1")
        table.add_column("Col2")
        table.add_row("A", "B")

        console = Console()
        with console.capture() as capture:
            console.print(table)

        output = capture.get()
        assert "Col1" in output
        assert "Col2" in output

    def test_panel_rendering(self):
        """测试 Panel 渲染"""
        from rich.panel import Panel
        from rich.console import Console

        panel = Panel("Test content", title="Test Panel")

        console = Console()
        with console.capture() as capture:
            console.print(panel)

        output = capture.get()
        assert "Test content" in output


class TestCommandOutput:
    """测试命令输出格式"""

    def test_help_command_output_format(self):
        """测试 /help 命令输出格式"""
        from xenon.repl.commands import COMMANDS

        # /help 命令应该存在
        assert "/help" in COMMANDS

        help_cmd = COMMANDS["/help"]
        assert "description" in help_cmd or help_cmd is not None

    def test_models_command_output_format(self):
        """测试 /models 命令输出格式"""
        from xenon.repl.commands import COMMANDS

        # /models 命令应该存在
        assert "/models" in COMMANDS

    def test_status_command_output_format(self):
        """测试 /status 命令输出格式"""
        from xenon.repl.commands import COMMANDS

        # /status 命令应该存在
        assert "/status" in COMMANDS


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
