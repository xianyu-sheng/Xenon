"""
Status Bar — 底部状态栏 (v0.5.5 · Reasonix 风格增强)。

在终端底部实时显示：
- 当前模型 / auto-routing
- Token 使用量（进度条）
- 思考范式
- 工具调用计数
- 会话时长
- 缓存命中率 / 费用（需 UsageTracker）
"""

from __future__ import annotations

import shutil
import time as _time
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

if TYPE_CHECKING:
    from xenon.repl.context_manager import ContextManager
    from xenon.repl.model_registry import ModelRegistry


class StatusBar:
    """Reasonix 风格底部状态栏。"""

    def __init__(
        self,
        console: Console,
        ctx_mgr: ContextManager,
        registry: ModelRegistry,
        *,
        usage_tracker: Any = None,
        cache_tracker: Any = None,  # CacheTracker（DeepSeek 缓存追踪）
    ) -> None:
        self.console = console
        self.ctx_mgr = ctx_mgr
        self.registry = registry
        self.usage_tracker = usage_tracker
        self.cache_tracker = cache_tracker
        self._streaming = True
        self._last_model: str | None = None
        self._auto_router = None
        self._notification: str | None = None
        self._notification_expires: float = 0.0
        self._session_start: float = _time.monotonic()
        self._tool_call_count: int = 0

    def set_last_model(self, model_id: str) -> None:
        self._last_model = model_id

    def set_streaming(self, enabled: bool) -> None:
        self._streaming = enabled

    def set_mode_notification(self, mode_name: str) -> None:
        self._notification = f"🔄{mode_name}"
        self._notification_expires = _time.monotonic() + 3.0

    def add_tool_call(self) -> None:
        self._tool_call_count += 1

    @property
    def tool_call_count(self) -> int:
        return self._tool_call_count

    @property
    def session_elapsed(self) -> float:
        return _time.monotonic() - self._session_start

    def _clear_expired_notification(self) -> None:
        if self._notification and _time.monotonic() > self._notification_expires:
            self._notification = None

    @staticmethod
    def _parse_pct(ratio) -> float:
        try:
            if isinstance(ratio, str):
                return float(ratio.strip('%'))
            return float(ratio) * 100 if ratio <= 1 else float(ratio)
        except (ValueError, TypeError, AttributeError):
            return 0.0

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        if m >= 60:
            h, m = divmod(m, 60)
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    # ── 主渲染 ─────────────────────────────────────────────

    def render(self) -> Panel:
        try:
            return self._render_impl()
        except Exception:
            return Panel("[dim]状态不可用[/dim]", style="dim", height=1, padding=(0, 1))

    def _render_impl(self) -> Panel:
        stats = self.ctx_mgr.stats()
        mode = self.registry.get_current_mode()
        used = stats["estimated_tokens"]
        max_tok = stats["max_tokens"]
        pct_val = self._parse_pct(stats["usage_ratio"])

        bar_width = 20
        filled = min(int(pct_val / 100 * bar_width), bar_width)
        bar_color = "red" if pct_val > 80 else ("yellow" if pct_val > 50 else "green")
        bar = f"[{bar_color}]{'█' * filled}[/{bar_color}][dim]{'░' * (bar_width - filled)}[/dim]"

        if self._auto_router and not self._auto_router.is_empty():
            active = self._auto_router.get_active_model_id() or self._last_model
            model_display = f"[bold green]auto[/bold green] {active or '—'}"
        else:
            model_display = self._last_model or "未设置"
        if len(model_display) > 25:
            model_display = "..." + model_display[-22:]

        stream_icon = "⚡流式" if self._streaming else "⏸阻塞"
        status_parts: list[str] = []
        if stats["needs_compact"]:
            status_parts.append("[bold red]⚠需压缩[/bold red]")
        status_parts.extend([
            f"[bold cyan]模型:[/bold cyan] {escape(model_display)}",
            f"[bold cyan]范式:[/bold cyan] {mode.name}",
            f"[bold cyan]Token:[/bold cyan] {bar} {used:,}/{max_tok:,} ({stats['usage_ratio']})",
        ])
        if self._tool_call_count > 0:
            status_parts.append(f"[bold cyan]🔧[/bold cyan] {self._tool_call_count}")
        status_parts.extend([
            f"[bold cyan]消息:[/bold cyan] {stats['total_messages']}",
            f"[bold cyan]时长:[/bold cyan] {self._fmt_duration(self.session_elapsed)}",
            f"[bold cyan]{stream_icon}[/bold cyan]",
        ])
        if stats["undo_available"] > 0:
            status_parts.append(f"[dim]↩×{stats['undo_available']}[/dim]")

        self._clear_expired_notification()
        notification_line = f"[bold yellow]{self._notification}[/bold yellow]  " if self._notification else ""
        return Panel(notification_line + "  │  ".join(status_parts), style="dim", height=1, padding=(0, 1))

    # ── prompt_toolkit bottom_toolbar ───────────────────────

    def get_toolbar_text(self) -> str:
        """Return a plain-text toolbar for callers outside prompt_toolkit."""
        try:
            return self._toolbar_impl()
        except Exception:
            return "状态不可用"

    def get_toolbar_fragments(self) -> list[tuple[str, str]]:
        """Return styled prompt_toolkit fragments for the interactive toolbar.

        Keeping visual treatment here (rather than embedding ANSI or Rich markup
        in a string) makes the toolbar render consistently on Windows Terminal,
        iTerm, and the plain prompt_toolkit fallback.
        """
        try:
            stats = self.ctx_mgr.stats()
            mode = self.registry.get_current_mode()
            pct = self._parse_pct(stats["usage_ratio"])
            model = self._last_model or "未设置"
            if self._auto_router and not self._auto_router.is_empty():
                model = f"auto · {self._auto_router.get_active_model_id() or model}"
            if len(model) > 28:
                model = "…" + model[-27:]

            state_class = (
                "class:toolbar.danger" if stats["needs_compact"] else "class:toolbar.muted"
            )
            usage_class = (
                "class:toolbar.danger" if pct > 80 else
                "class:toolbar.warning" if pct > 50 else
                "class:toolbar.good"
            )
            fragments = [
                ("class:toolbar.brand", "  XENON "),
                ("class:toolbar.separator", "│"),
                ("class:toolbar.model", f"  {model}  "),
                ("class:toolbar.separator", "│"),
                ("class:toolbar.mode", f"  {mode.name}  "),
                ("class:toolbar.separator", "│"),
                (usage_class, f"  context {stats['usage_ratio']}  "),
                ("class:toolbar.separator", "│"),
                ("class:toolbar.muted", f"  {stats['total_messages']} messages · {self._fmt_duration(self.session_elapsed)}  "),
            ]
            if self._tool_call_count:
                fragments.extend([("class:toolbar.separator", "│"), ("class:toolbar.muted", f"  🔧 {self._tool_call_count}  ")])
            if stats["needs_compact"]:
                fragments.extend([("class:toolbar.separator", "│"), (state_class, "  ⚠ /compact  ")])
            if self._notification:
                fragments.extend([("class:toolbar.separator", "│"), ("class:toolbar.notice", f"  {self._notification}  ")])
            return fragments
        except Exception:
            return [("class:toolbar.danger", "  状态不可用  ")]

    def _toolbar_impl(self) -> str:
        stats = self.ctx_mgr.stats()
        mode = self.registry.get_current_mode()
        pct_val = self._parse_pct(stats["usage_ratio"])

        if self._auto_router and not self._auto_router.is_empty():
            active = self._auto_router.get_active_model_id() or self._last_model
            model_display = f"auto {active or '—'}"
        else:
            model_display = self._last_model or "—"
        if len(model_display) > 35:
            model_display = "…" + model_display[-34:]

        bar_width = 8
        filled = min(int(pct_val / 100 * bar_width), bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)

        self._clear_expired_notification()
        notify = f"{self._notification} · " if self._notification else ""
        warn = "⚠ /compact · " if stats["needs_compact"] else ""
        stream = "⚡" if self._streaming else "⏸"

        # ── 三段式布局：左(缓存) · 中(模型/范式) · 右(Token/消息/时长) ──
        term_width = shutil.get_terminal_size().columns

        # 左段：缓存数据（最重要）
        left_parts: list[str] = []
        if self.cache_tracker:
            cr = self.cache_tracker
            total_cache = cr.cache_hits + cr.cache_misses
            if total_cache > 0:
                left_parts.append(f"💾{cr.cache_hit_rate:.0%}")
            if cr.estimated_cost_yuan > 0:
                cost = cr.estimated_cost_yuan
                left_parts.append(f"💰{'<0.01' if cost < 0.01 else f'{cost:.2f}'}")
                if cr.savings_pct >= 1:
                    left_parts.append(f"💡{cr.savings_pct}%")
        if not left_parts:
            left = ""
        else:
            left = " ".join(left_parts)

        # 中段：模型名 + 范式 + 警告
        center_parts = [model_display, mode.name]
        if warn:
            center_parts.insert(0, warn.rstrip(" · "))
        center = " · ".join(center_parts)

        # 右段：Token 条 + 消息数 + 工具数 + 时长 + 流式状态
        right_parts = [f"{bar} {stats['usage_ratio']}"]
        if self._tool_call_count > 0:
            right_parts.append(f"🔧{self._tool_call_count}")
        right_parts.append(f"{stats['total_messages']}m")
        right_parts.append(self._fmt_duration(self.session_elapsed))
        right_parts.append(stream)
        right = " · ".join(right_parts)

        # 三段式排版：左 → 中 ← 右，自适应间距
        left_w = len(left)
        right_w = len(right)
        center_w = len(center)
        available = term_width - left_w - right_w - 4  # 保留两个 "  " 分隔
        if available >= center_w:
            padding = available - center_w
            left_pad = padding // 2
            right_pad = padding - left_pad
            line = f"{left}  {' ' * left_pad}{center}{' ' * right_pad}  {right}"
        else:
            line = f"{left}  {center}  {right}"
            if len(line) > term_width:
                line = line[:term_width - 1]

        return line.rstrip()

    # ── 非 PT 模式 ─────────────────────────────────────────

    def print_status(self) -> None:
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
        pct_val = self._parse_pct(stats["usage_ratio"])

        if self._auto_router and not self._auto_router.is_empty():
            active = self._auto_router.get_active_model_id() or self._last_model
            model_display = f"auto {active or '—'}"
        else:
            model_display = self._last_model or "—"
        if len(model_display) > 30:
            model_display = "..." + model_display[-27:]

        token_color = "red" if pct_val > 80 else ("yellow" if pct_val > 50 else "green")
        stream = "⚡" if self._streaming else "⏸"

        self._clear_expired_notification()
        notify = f"[bold yellow]{self._notification}[/bold yellow] · " if self._notification else ""
        warn = "[bold red]⚠ 建议 /compact[/bold red] · " if stats["needs_compact"] else ""
        tool_part = f"🔧 {self._tool_call_count} · " if self._tool_call_count > 0 else ""
        dur = self._fmt_duration(self.session_elapsed)

        # 缓存数据（与 PT toolbar 一致）
        cache_part = ""
        if self.cache_tracker:
            cr = self.cache_tracker
            total_cache = cr.cache_hits + cr.cache_misses
            if total_cache > 0:
                cache_part = f"💾{cr.cache_hit_rate:.0%} "
                if cr.estimated_cost_yuan > 0:
                    cost = cr.estimated_cost_yuan
                    cache_part += f"💰{'<0.01' if cost < 0.01 else f'{cost:.2f}'} "
                    if cr.savings_pct >= 1:
                        cache_part += f"💡{cr.savings_pct}% · "

        line = (
            f"[dim]  {cache_part}{notify}{warn}{escape(model_display)} · {mode.name} · "
            f"[{token_color}]Token {stats['estimated_tokens']:,}/{stats['max_tokens']:,} ({stats['usage_ratio']})[/{token_color}] · "
            f"{tool_part}消息 {stats['total_messages']} · {dur} · {stream}"
        ) + "[/dim]"
        self.console.print(line)
