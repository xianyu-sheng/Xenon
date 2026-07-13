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

import time as _time
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
        self._auto_router = None  # v0.4.0: set by REPL
        self._notification: str | None = None  # v0.4.0: 一次性通知
        self._notification_expires: float = 0.0

    def set_last_model(self, model_id: str) -> None:
        """记录最近一次使用的模型。"""
        self._last_model = model_id

    def set_streaming(self, enabled: bool) -> None:
        self._streaming = enabled

    def set_mode_notification(self, mode_name: str) -> None:
        """设置一次性模式切换通知（3 秒后自动清除）。"""
        self._notification = f"🔄 切换至: {mode_name}"
        self._notification_expires = _time.monotonic() + 3.0

    def _clear_expired_notification(self) -> None:
        if self._notification and _time.monotonic() > self._notification_expires:
            self._notification = None

    @staticmethod
    def _parse_pct(ratio) -> float:
        """解析 '85.0%' / 0.85 / None → 百分比浮点。"""
        try:
            if isinstance(ratio, str):
                return float(ratio.strip('%'))
            return float(ratio) * 100 if ratio <= 1 else float(ratio)
        except (ValueError, TypeError, AttributeError):
            return 0.0

    def _fallback_panel(self, hint: str = "状态不可用") -> Panel:
        """render 异常时的降级面板（§8.18.1）。"""
        return Panel(f"[dim]{hint}[/dim]", style="dim", height=1, padding=(0, 1))

    def render(self) -> Panel:
        """渲染状态栏内容。

        P3-Q10 / §8.18.1：整体 try/except 兜底——stats 下标/字段异常时不让 Live 崩，
        返回固定"状态不可用"面板。
        P3-Q10 / §8.18.2：``⚠需压缩`` 警告置于状态行**首位**，窄屏截断只丢次要信息，
        提示 /compact 的核心信号不被吃掉。
        """
        try:
            return self._render_impl()
        except Exception:
            return self._fallback_panel()

    def _render_impl(self) -> Panel:
        stats = self.ctx_mgr.stats()
        mode = self.registry.get_current_mode()

        # Token 使用量
        used = stats["estimated_tokens"]
        max_tok = stats["max_tokens"]
        ratio = stats["usage_ratio"]
        bar_width = 20

        pct_val = self._parse_pct(ratio)
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
        # v0.4.0: auto-routing indicator
        if self._auto_router and not self._auto_router.is_empty():
            active = self._auto_router.get_active_model_id() or self._last_model
            model_display = f"[bold green]auto[/bold green] {active or '—'}"
        else:
            model_display = self._last_model or "未设置"
        if len(model_display) > 25:
            model_display = "..." + model_display[-22:]

        # 流式状态
        stream_icon = "⚡流式" if self._streaming else "⏸阻塞"

        # 组装状态行——⚠需压缩 置首，窄屏截断只丢末尾次要项（§8.18.2）
        status_parts: list[str] = []
        if stats["needs_compact"]:
            status_parts.append("[bold red]⚠需压缩[/bold red]")
        status_parts.extend([
            f"[bold cyan]模型:[/bold cyan] {model_display}",
            f"[bold cyan]范式:[/bold cyan] {mode.name}",
            f"[bold cyan]Token:[/bold cyan] {bar} {used:,}/{max_tok:,} ({ratio})",
            f"[bold cyan]消息:[/bold cyan] {stats['total_messages']}",
            f"[bold cyan]{stream_icon}[/bold cyan]",
        ])

        if stats["undo_available"] > 0:
            status_parts.append(f"[dim]↩×{stats['undo_available']}[/dim]")

        # v0.4.0: 通知横幅
        self._clear_expired_notification()
        notification_line = ""
        if self._notification:
            notification_line = f"[bold yellow]{self._notification}[/bold yellow]  "

        content = notification_line + "  │  ".join(status_parts)

        return Panel(
            content,
            style="dim",
            height=1,
            padding=(0, 1),
        )

    def print_status(self) -> None:
        """打印一行紧凑的状态信息（非 Live 模式）。

        P3-Q10 / §8.18.1：整体 try/except 兜底，异常时打印降级提示不崩。
        """
        try:
            self._print_status_impl()
        except Exception:
            try:
                self.console.print("[dim]状态不可用[/dim]")
            except Exception:
                pass

    def _print_status_impl(self) -> None:
        stats = self.ctx_mgr.stats()
        mode = self.registry.get_current_mode()

        used = stats["estimated_tokens"]
        max_tok = stats["max_tokens"]
        ratio = stats["usage_ratio"]

        if self._auto_router and not self._auto_router.is_empty():
            active = self._auto_router.get_active_model_id() or self._last_model
            model_display = f"auto {active or '—'}"
        else:
            model_display = self._last_model or "—"
        if len(model_display) > 30:
            model_display = "..." + model_display[-27:]

        stream = "⚡" if self._streaming else "⏸"

        pct_val = self._parse_pct(ratio)

        if pct_val > 80:
            token_color = "red"
        elif pct_val > 50:
            token_color = "yellow"
        else:
            token_color = "green"

        # v0.4.0: 通知横幅
        self._clear_expired_notification()
        notify = ""
        if self._notification:
            notify = f"[bold yellow]{self._notification}[/bold yellow] │ "

        # ⚠建议 /compact 置首，保证可见（§8.18.2）
        warn = "[bold red]⚠ 建议 /compact[/bold red] │ " if stats["needs_compact"] else ""

        line = (notify +
            f"[dim]┌─ {warn}{model_display} │ {mode.name} │ "
            f"[{token_color}]Token {used:,}/{max_tok:,} ({ratio})[/{token_color}] │ "
            f"消息 {stats['total_messages']} │ {stream}"
        )
        line += "[/dim]"

        self.console.print(line)
