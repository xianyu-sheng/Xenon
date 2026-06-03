"""
Status Bar — 底部状态栏。

在终端底部实时显示：
- 当前模型
- Token 使用量（进度条）
- 思˄范式
- 流式模式
- 对话轮次
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from omniagent.repl.context_manager import ContextManager
    from omniagent.repl.model_registry import ModelRegistry


class StatusBar:
    """
    底部状态栏管理器。

    使用 Rich Live 实现实时刷新，在终端底部显示上下文状态。
    """

    def __init__(
        self,
        console: Console,
        ctx_mgr: ContextManager,
        registry: ModelRegistry,
    ) -> None:
        self.console = console
        self.ctx_mgr = ctx_mgr
        self.registry = registry
        self._streaming = True
        self._last_model: str | None = None

    def set_last_model(self, model_id: str) -> None:
        """记录最近一次使用的模型。"""
        self._last_model = model_id

    def set_streaming(self, enabled: bool) -> None:
        self._streaming = enabled

    def render(self) -> Panel:
        """渲染状态栏内容。"""
        stats = self.ctx_mgr.stats()
        mode = self.registry.get_current_mode()

        # Token 使用量
        used = stats["estimated_tokens"]
        max_tok = stats["max_tokens"]
        ratio = stats["usage_ratio"]
        bar_width = 20

        # 进度条
        filled = min(int(float(ratio.strip('%')) / 100 * bar_width), bar_width)
        # 解析百分比数值
        try:
            pct_val = float(ratio.strip('%'))
        except (ValueError, AttributeError):
            pct_val = 0.0
        filled = min(int(pct_val / 100 * bar_width), bar_width)
        empty = bar_width - filled

        if pct_val > 80:
            bar_color = "red"
        elif pct_val > 50:
            bar_color = "yellow"
        else:
            bar_color = "green"

        bar = f"[{bar_color}]{'█' * filled}[/{bar_color}][dim]{'░' * empty}[/dim]"

        # 当前模型
        model_display = self._last_model or "未设置"
        if len(model_display) > 25:
            model_display = "..." + model_display[-22:]

        # 流式状态
        stream_icon = "⚡流式" if self._streaming else "⏸阻塞"

        # 组装状态行
        status_parts = [
            f"[bold cyan]模型:[/bold cyan] {model_display}",
            f"[bold cyan]范式:[/bold cyan] {mode.name}",
            f"[bold cyan]Token:[/bold cyan] {bar} {used:,}/{max_tok:,} ({ratio})",
            f"[bold cyan]消息:[/bold cyan] {stats['total_messages']}",
            f"[bold cyan]{stream_icon}[/bold cyan]",
        ]

        if stats["undo_available"] > 0:
            status_parts.append(f"[dim]↩×{stats['undo_available']}[/dim]")

        if stats["needs_compact"]:
            status_parts.append("[bold red]⚠需压缩[/bold red]")

        content = "  │  ".join(status_parts)

        return Panel(
            content,
            style="dim",
            height=1,
            padding=(0, 1),
        )

    def print_status(self) -> None:
        """打印一行紧凑的状态信息（非 Live 模式）。"""
        stats = self.ctx_mgr.stats()
        mode = self.registry.get_current_mode()

        used = stats["estimated_tokens"]
        max_tok = stats["max_tokens"]
        ratio = stats["usage_ratio"]

        model_display = self._last_model or "—"
        if len(model_display) > 30:
            model_display = "..." + model_display[-27:]

        stream = "⚡" if self._streaming else "⏸"

        # 紧凑单行
        try:
            pct_val = float(ratio.strip('%'))
        except (ValueError, AttributeError):
            pct_val = 0.0

        if pct_val > 80:
            token_color = "red"
        elif pct_val > 50:
            token_color = "yellow"
        else:
            token_color = "green"

        line = (
            f"[dim]┌─ {model_display} │ {mode.name} │ "
            f"[{token_color}]Token {used:,}/{max_tok:,} ({ratio})[/{token_color}] │ "
            f"消息 {stats['total_messages']} │ {stream}"
        )
        if stats["needs_compact"]:
            line += " │ [bold red]⚠ 建议 /compact[/bold red]"
        line += "[/dim]"

        self.console.print(line)
