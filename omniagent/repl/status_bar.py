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
    增强版：显示迭代计数、引擎状态、always-approved 徽章。
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
        # 引擎状态追踪
        self._iteration: int = 0
        self._max_iterations: int = 0
        self._engine_status: str = "idle"  # idle | running | thinking | done
        # 权限缓存引用（由 REPL 注入）
        self._always_approved_count: int = 0

    def set_last_model(self, model_id: str) -> None:
        """记录最近一次使用的模型。"""
        self._last_model = model_id

    def set_streaming(self, enabled: bool) -> None:
        self._streaming = enabled

    def set_iteration(self, iteration: int, total: int) -> None:
        """设置当前引擎迭代进度。"""
        self._iteration = iteration
        self._max_iterations = total

    def set_engine_status(self, status: str) -> None:
        """设置引擎状态: idle | running | thinking | acting | done。"""
        self._engine_status = status

    def set_always_approved_count(self, count: int) -> None:
        """设置当前会话的 always-approved 计数。"""
        self._always_approved_count = count

    def _engine_status_icon(self) -> str:
        """获取引擎状态图标。"""
        return {
            "idle": "",
            "running": "[cyan]◌[/cyan]",
            "thinking": "[dim]💭[/dim]",
            "acting": "[yellow]🔧[/yellow]",
            "done": "[green]✅[/green]",
        }.get(self._engine_status, "")

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
        """打印极氪风格紧凑状态行（增强版：含迭代/引擎状态/授权徽章）。"""
        stats = self.ctx_mgr.stats()
        mode = self.registry.get_current_mode()

        used = stats["estimated_tokens"]
        max_tok = stats["max_tokens"]
        ratio = stats["usage_ratio"]

        model_display = self._last_model or "—"
        if len(model_display) > 30:
            model_display = "..." + model_display[-27:]

        stream_icon = "⚡" if self._streaming else "⏸"

        try:
            pct_val = float(ratio.strip('%'))
        except (ValueError, AttributeError):
            pct_val = 0.0

        if pct_val > 80:
            token_style = "bold red"
        elif pct_val > 50:
            token_style = "yellow"
        else:
            token_style = "bright_cyan"

        # 极氪风格：简洁分隔，突出关键信息
        line = (
            f"[dim]▎[/dim] "
            f"[bold bright_cyan]{model_display}[/bold bright_cyan]"
            f" [dim]·[/dim] "
            f"{mode.name}"
            f" [dim]·[/dim] "
            f"[{token_style}]▐{'█' * min(int(pct_val / 100 * 8), 8)}{'░' * max(8 - int(pct_val / 100 * 8), 0)}▌ {used:,}/{max_tok:,}[/{token_style}]"
            f" [dim]·[/dim] "
            f"✉ {stats['total_messages']}"
            f"  {stream_icon}"
        )

        # 引擎迭代状态
        if self._max_iterations > 0:
            line += f" [dim]· 🔄 {self._iteration}/{self._max_iterations}[/dim]"
        elif self._engine_status not in ("idle", "done"):
            line += f"  {self._engine_status_icon()}"

        # always-approved 徽章
        if self._always_approved_count > 0:
            line += f" [dim cyan]· A{self._always_approved_count}[/dim cyan]"

        if stats["needs_compact"]:
            line += " [bold red]⚠ 需 /compact[/bold red]"

        self.console.print(line)
