"""Portable terminal-tab activity for Xenon's interactive REPL.

Terminal emulators do not expose a shared protocol for animating a bitmap tab
icon.  They do, however, widely support updating the tab/window title.  Xenon
therefore renders the Star Core identity as a fixed-width three-cell starfield:

    ✶·· Xenon  ->  ·✶· Xenon  ->  ··✶ Xenon

Only the title changes; no cursor movement or terminal body output is involved.
The animation is deliberately stopped whenever Xenon is waiting for input.
"""

from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, TextIO


class TerminalActivityState(Enum):
    """User-visible execution states for the terminal tab."""

    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    INTERRUPTED = "interrupted"
    ERROR = "error"


_UNICODE_FRAMES: tuple[str, ...] = (
    "✶·· Xenon",
    "·✶· Xenon",
    "··✶ Xenon",
    "·✧✶ Xenon",
    "✧✶· Xenon",
)
_ASCII_FRAMES: tuple[str, ...] = (
    "*.. Xenon",
    ".*. Xenon",
    "..* Xenon",
    ".+* Xenon",
    "+*. Xenon",
)


class TerminalActivityIndicator:
    """Animate Xenon's portable terminal-tab title while work is in flight.

    The worker thread is created lazily on the first RUNNING transition and is
    always daemonized.  Static transitions are written immediately, so a
    permission prompt cannot leave a stale moving title behind.
    """

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        interval: float = 0.25,
        enabled: bool | None = None,
        ascii_only: bool | None = None,
        writer: Callable[[str], None] | None = None,
        original_title: str | None = None,
    ) -> None:
        self._stream = stream or sys.stdout
        self._interval = max(0.08, float(interval))
        self._enabled = self._detect_enabled() if enabled is None else bool(enabled)
        self._ascii_only = (
            os.environ.get("XENON_TERMINAL_ASCII") == "1"
            if ascii_only is None
            else bool(ascii_only)
        )
        self._frames = _ASCII_FRAMES if self._ascii_only else _UNICODE_FRAMES
        self._writer = writer or self._write_platform_title
        self._original_title = (
            original_title
            or os.environ.get("XENON_ORIGINAL_TERMINAL_TITLE")
            or self._capture_windows_title()
        )

        self._condition = threading.Condition(threading.RLock())
        self._state = TerminalActivityState.IDLE
        self._frame_index = 0
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def state(self) -> TerminalActivityState:
        with self._condition:
            return self._state

    def start(self) -> None:
        """Publish the static idle identity without starting a worker thread."""
        self.set_state(TerminalActivityState.IDLE)

    def set_state(
        self,
        state: TerminalActivityState,
        *,
        detail: str | None = None,
    ) -> None:
        """Switch state and synchronously publish its first/static title."""
        if not isinstance(state, TerminalActivityState):
            raise TypeError("state must be a TerminalActivityState")
        if not self._enabled:
            return

        with self._condition:
            if self._closed:
                return
            self._state = state
            self._frame_index = 0
            if state is TerminalActivityState.RUNNING:
                self._emit_locked(self._frames[0])
                if not self._enabled:
                    return
                self._frame_index = 1
                self._ensure_thread_locked()
            else:
                self._emit_locked(self._static_title(state, detail))
            self._condition.notify_all()

    def idle(self) -> None:
        self.set_state(TerminalActivityState.IDLE)

    def interrupted(self) -> None:
        self.set_state(TerminalActivityState.INTERRUPTED)

    def error(self) -> None:
        self.set_state(TerminalActivityState.ERROR)

    @contextmanager
    def active(self) -> Iterator[None]:
        """Run a top-level task with animation, then return to a still title."""
        previous = self.state
        self.set_state(TerminalActivityState.RUNNING)
        try:
            yield
        finally:
            # Nested activity keeps its parent's running state.  Top-level work
            # always settles to idle after completion or cancellation.
            target = (
                TerminalActivityState.RUNNING
                if previous is TerminalActivityState.RUNNING
                else TerminalActivityState.IDLE
            )
            self.set_state(target)

    @contextmanager
    def waiting(self, detail: str = "等待确认") -> Iterator[None]:
        """Freeze the starfield while Xenon is blocked on a user decision."""
        previous = self.state
        self.set_state(TerminalActivityState.WAITING, detail=detail)
        try:
            yield
        finally:
            self.set_state(previous)

    def close(self) -> None:
        """Stop the worker and restore the prior/best-effort shell title."""
        thread: threading.Thread | None
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._state = TerminalActivityState.IDLE
            self._condition.notify_all()
            if self._enabled:
                restore = self._original_title or self._fallback_shell_title()
                self._emit_locked(restore)
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self._interval * 4))

    def _detect_enabled(self) -> bool:
        if os.environ.get("XENON_TERMINAL_ANIMATION") == "0":
            return False
        if os.environ.get("TERM", "").lower() == "dumb":
            return False
        if os.environ.get("CI"):
            return False
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._animate,
            name="xenon-terminal-title",
            daemon=True,
        )
        self._thread.start()

    def _animate(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._closed
                    or self._state is TerminalActivityState.RUNNING
                )
                if self._closed:
                    return
                title = self._frames[self._frame_index % len(self._frames)]
                self._frame_index += 1
                self._emit_locked(title)
                if not self._enabled:
                    return
                self._condition.wait(timeout=self._interval)

    def _static_title(
        self,
        state: TerminalActivityState,
        detail: str | None,
    ) -> str:
        if self._ascii_only:
            if state is TerminalActivityState.IDLE:
                return self._frames[0]
            marker = "!" if state is TerminalActivityState.ERROR else "-"
        else:
            if state is TerminalActivityState.IDLE:
                return self._frames[0]
            marker = "✶" if state is TerminalActivityState.ERROR else "☆"

        default_details = {
            TerminalActivityState.WAITING: "等待确认",
            TerminalActivityState.INTERRUPTED: "已中断",
            TerminalActivityState.ERROR: "出错",
        }
        suffix = self._sanitize(detail or default_details.get(state, ""))
        return f"{marker} Xenon" + (f" · {suffix}" if suffix else "")

    def _emit_locked(self, title: str) -> None:
        try:
            self._writer(self._sanitize(title))
        except Exception:
            # A terminal-title enhancement must never affect agent execution.
            self._enabled = False

    @staticmethod
    def _sanitize(title: str) -> str:
        return (
            str(title)
            .replace("\x1b", "")
            .replace("\x07", "")
            .replace("\r", " ")
            .replace("\n", " ")
        )[:96]

    def _write_platform_title(self, title: str) -> None:
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(title)
            return
        self._stream.write(f"\x1b]0;{title}\x07")
        self._stream.flush()

    @staticmethod
    def _capture_windows_title() -> str | None:
        if sys.platform != "win32":
            return None
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(512)
            length = ctypes.windll.kernel32.GetConsoleTitleW(buffer, len(buffer))
            return buffer.value if length else None
        except Exception:
            return None

    @staticmethod
    def _fallback_shell_title() -> str:
        """Portable fallback when Unix terminals cannot report the old title."""
        try:
            directory = Path.cwd().name
        except OSError:
            directory = ""
        shell = Path(os.environ.get("SHELL", "")).name
        if directory and shell:
            return f"{directory} — {shell}"
        return directory or shell or "Terminal"
