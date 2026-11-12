"""
Status Bar — 输入区底部状态栏。

在终端底部实时显示：
- 当前模型 / auto-routing
- Token 使用量（进度条）
- 模式
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


def _format_window(tokens: int) -> str:
    if tokens >= 1_000_000:
        value = tokens / 1_000_000
        return f"{value:.0f}M" if abs(value - round(value)) < 0.05 else f"{value:.1f}M"
    if tokens >= 1000:
        return f"{round(tokens / 1000)}K"
    return str(tokens)


class StatusBar:
    """轻量、自适应的输入区状态栏。"""

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
        # The loop a kernel turn is executing.  ``/mode`` is chat
        # configuration; showing it while a task runs would claim a mode the
        # agent is not using (``direct`` was shown during react-style turns).
        self._active_loop: str | None = None
        self._auto_router = None
        self._notification: str | None = None
        self._notification_expires: float = 0.0
        self._session_start: float = _time.monotonic()
        self._tool_call_count: int = 0
        # #372 E1: the live kernel snapshot (phase/steps/tokens/stalled), set
        # by the runtime's own progress hook.  None means no kernel turn is
        # running — the bar then shows /mode config, never stale "last loop".
        self._kernel_snapshot: dict[str, Any] | None = None

    def set_last_model(self, model_id: str) -> None:
        self._last_model = model_id
        # Problem 5: 状态变更时刷新
        self.refresh()

    def set_active_loop(self, name: str | None) -> None:
        """Show the loop actually executing, or ``None`` to fall back to mode."""

        self._active_loop = name or None
        # Problem 5: 状态变更时刷新
        self.refresh()

    def set_kernel_status(self, snapshot: dict[str, Any] | None) -> None:
        """#372 E1: live kernel state from the runtime, verbatim."""

        self._kernel_snapshot = snapshot
        self.refresh()

    def set_streaming(self, enabled: bool) -> None:
        self._streaming = enabled
        # Problem 5: 状态变更时刷新
        self.refresh()

    def set_mode_notification(self, mode_name: str) -> None:
        self._notification = f"🔄{mode_name}"
        self._notification_expires = _time.monotonic() + 3.0
        # Problem 5: 状态变更时刷新
        self.refresh()

    def add_tool_call(self) -> None:
        self._tool_call_count += 1
        # Problem 5: 状态变更时刷新
        self.refresh()

    @property
    def tool_call_count(self) -> int:
        return self._tool_call_count

    @property
    def session_elapsed(self) -> float:
        return _time.monotonic() - self._session_start

    def _clear_expired_notification(self) -> None:
        if self._notification and _time.monotonic() > self._notification_expires:
            self._notification = None

    # ── Problem 5: 状态刷新 ─────────────────────────────

    def refresh(self) -> None:
        """刷新状态栏显示（由状态变更点调用）。

        状态是拉取式的：``print_status`` 在每轮开始前重画，
        所以这里只是一个变更钩子，不打印任何内容。
        """
        pass

    def _preferred_model_id(self) -> str | None:
        """用户通过 /set_model、/model 或 -m 显式选择的模型。

        优先于池序的 healthy[0]：否则用户切换后底栏仍显示旧模型。
        """

        router = self._auto_router
        if router is None:
            return None
        try:
            preferred = getattr(router, "_preferred_model_ids", None)
        except Exception:  # noqa: BLE001 — 显示不能阻断
            return None
        if preferred:
            return str(preferred[0])
        return None

    def _display_mode(self) -> Any:
        """The loop actually running, else the configured chat mode."""

        active = self._active_loop
        if active:
            return type("_Loop", (), {"name": active})()
        return self.registry.get_current_mode()

    def _model_for_display(self) -> str:
        """显示模型：用户选择 > auto 路由活动模型 > 最近成功模型 > 未设置。"""

        preferred = self._preferred_model_id()
        if preferred:
            return preferred
        if self._auto_router and not self._auto_router.is_empty():
            active = self._auto_router.get_active_model_id()
            if active:
                return f"auto · {active}"
        return self._last_model or "未设置"

    @staticmethod
    def _truncate_model(model: str, limit: int) -> str:
        """截断模型名时保留 ``auto ·`` 前缀，否则字面量会先被切掉。"""

        if len(model) <= limit:
            return model
        prefix = "auto · " if model.startswith("auto · ") else ""
        body = model[len(prefix) :]
        keep = max(1, limit - len(prefix) - 1)
        return f"{prefix}…{body[-keep:]}"

    def _cache_badge(self) -> tuple[str, str] | None:
        if not self.cache_tracker:
            return None
        return self.cache_tracker.cache_badge

    @staticmethod
    def _parse_pct(ratio) -> float:
        """解析百分比值，返回0.0-1.0范围的比例值。"""
        try:
            if isinstance(ratio, str):
                # "50%" → 0.5
                return float(ratio.strip("%")) / 100
            val = float(ratio)
            # 75 → 0.75, 0.5 → 0.5
            return val / 100 if val > 1 else val
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

        model_display = self._truncate_model(self._model_for_display(), 25)

        stream_icon = "⚡流式" if self._streaming else "⏸阻塞"
        status_parts: list[str] = []
        if stats["needs_compact"]:
            status_parts.append("[bold red]⚠需压缩[/bold red]")
        status_parts.extend(
            [
                f"[bold cyan]模型:[/bold cyan] {escape(model_display)}",
                f"[bold cyan]模式:[/bold cyan] {mode.name}",
                f"[bold cyan]Token:[/bold cyan] {bar} {used:,}/{max_tok:,} ({stats['usage_ratio']})",
            ]
        )
        if self._tool_call_count > 0:
            status_parts.append(f"[bold cyan]🔧[/bold cyan] {self._tool_call_count}")
        if self._kernel_snapshot is not None:
            # #372 E1: live kernel facts, rendered verbatim from the runtime.
            snap = self._kernel_snapshot
            kernel_text = (
                f"step {int(snap.get('steps') or 0)} · "
                f"{snap.get('phase') or '?'} · "
                f"tool {int(snap.get('tool_calls') or 0)}"
            )
            stalled = float(snap.get("stalled_seconds") or 0)
            if stalled > 0:
                kernel_text += f" · 停滞 {stalled:.0f}s"
            status_parts.append(f"[bold cyan]内核[/bold cyan] {kernel_text}")
        cache_badge = self._cache_badge()
        if cache_badge:
            cache_text, cache_style = cache_badge
            rich_style = {
                "good": "green",
                "warning": "yellow",
                "danger": "red",
                "muted": "dim",
            }.get(cache_style, "dim")
            status_parts.append(f"[{rich_style}]{cache_text}[/{rich_style}]")
        status_parts.extend(
            [
                f"[bold cyan]消息:[/bold cyan] {stats['total_messages']}",
                f"[bold cyan]时长:[/bold cyan] {self._fmt_duration(self.session_elapsed)}",
                f"[bold cyan]{stream_icon}[/bold cyan]",
            ]
        )
        if stats["undo_available"] > 0:
            status_parts.append(f"[dim]↩×{stats['undo_available']}[/dim]")

        self._clear_expired_notification()
        notification_line = (
            f"[bold yellow]{self._notification}[/bold yellow]  "
            if self._notification
            else ""
        )
        return Panel(
            notification_line + "  │  ".join(status_parts),
            style="dim",
            height=1,
            padding=(0, 1),
        )

    # ── 一行文本状态（内置编辑器在提交后打印）───────────────

    def get_toolbar_text(self) -> str:
        """Return the one-line status used by the built-in editor."""
        try:
            return self._toolbar_impl()
        except Exception:
            return "状态不可用"

    def _toolbar_impl(self) -> str:
        stats = self.ctx_mgr.stats()
        mode = self._display_mode()
        pct_val = self._parse_pct(stats["usage_ratio"])

        model_display = self._truncate_model(self._model_for_display(), 35)

        bar_width = 8
        filled = min(int(pct_val / 100 * bar_width), bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)

        self._clear_expired_notification()
        warn = "⚠ /compact · " if stats["needs_compact"] else ""
        stream = "⚡" if self._streaming else "⏸"

        # ── 三段式布局：左(缓存) · 中(模型/模式) · 右(Token/消息/时长) ──
        term_width = shutil.get_terminal_size().columns

        # 左段：缓存数据（最重要）
        left_parts: list[str] = []
        if self.cache_tracker:
            cr = self.cache_tracker
            cache_text, _cache_style = cr.cache_badge
            left_parts.append(f"💾{cache_text.removeprefix('cache ')}")
            if cr.estimated_cost_yuan > 0:
                cost = cr.estimated_cost_yuan
                left_parts.append(f"💰{'<0.01' if cost < 0.01 else f'{cost:.2f}'}")
                if cr.savings_pct >= 1:
                    left_parts.append(f"💡{cr.savings_pct}%")
        if not left_parts:
            left = ""
        else:
            left = " ".join(left_parts)

        # 中段：模型名 + 模式 + 警告
        center_parts = [model_display, mode.name]
        if warn:
            while warn and warn[-1] in " ·":
                warn = warn[:-1]
            center_parts.insert(0, warn)
        center = " · ".join(center_parts)

        # 右段：Token 条 + 模型窗口 + 消息数 + 工具数 + 时长 + 流式状态
        window = int(stats.get("max_tokens") or 0)
        right_parts = [f"{bar} {stats['usage_ratio']}"]
        if window > 0:
            # Show the window the agent is actually sized against, so a 1M
            # model never reads as if it were 128K.
            right_parts.append(_format_window(window))
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
                line = line[: term_width - 1]

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
        mode = self._display_mode()
        pct_val = self._parse_pct(stats["usage_ratio"])

        model_display = self._truncate_model(self._model_for_display(), 30)

        token_color = "red" if pct_val > 80 else ("yellow" if pct_val > 50 else "green")
        stream = "⚡" if self._streaming else "⏸"

        self._clear_expired_notification()
        notify = (
            f"[bold yellow]{self._notification}[/bold yellow] · "
            if self._notification
            else ""
        )
        warn = (
            "[bold red]⚠ 建议 /compact[/bold red] · " if stats["needs_compact"] else ""
        )
        tool_part = (
            f"🔧 {self._tool_call_count} · " if self._tool_call_count > 0 else ""
        )
        dur = self._fmt_duration(self.session_elapsed)

        # 缓存数据（与 PT toolbar 一致）
        cache_part = ""
        if self.cache_tracker:
            cr = self.cache_tracker
            cache_text, _cache_style = cr.cache_badge
            cache_part = f"💾{cache_text.removeprefix('cache ')} "
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
