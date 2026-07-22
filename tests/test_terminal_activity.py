"""Tests for the terminal-tab Star Core activity state machine."""

from __future__ import annotations

import time

from xenon.repl.repl import REPL
from xenon.repl.terminal_activity import (
    TerminalActivityIndicator,
    TerminalActivityState,
)


def test_disabled_indicator_is_a_noop():
    titles: list[str] = []
    indicator = TerminalActivityIndicator(enabled=False, writer=titles.append)

    indicator.start()
    with indicator.active():
        pass
    indicator.close()

    assert titles == []


def test_running_animates_and_idle_stops():
    titles: list[str] = []
    indicator = TerminalActivityIndicator(
        enabled=True,
        interval=0.01,
        writer=titles.append,
        original_title="original",
    )

    indicator.start()
    indicator.set_state(TerminalActivityState.RUNNING)
    deadline = time.monotonic() + 1
    while "·✶· Xenon" not in titles and time.monotonic() < deadline:
        time.sleep(0.01)
    indicator.idle()
    settled_count = len(titles)
    time.sleep(0.05)

    assert titles[0] == "✶·· Xenon"
    assert "✶·· Xenon" in titles
    assert "·✶· Xenon" in titles
    assert titles[settled_count - 1] == "✶·· Xenon"
    assert len(titles) == settled_count

    indicator.close()
    assert titles[-1] == "original"


def test_waiting_freezes_then_resumes_parent_activity():
    titles: list[str] = []
    indicator = TerminalActivityIndicator(
        enabled=True,
        interval=1,
        writer=titles.append,
        original_title="original",
    )

    with indicator.active():
        assert indicator.state is TerminalActivityState.RUNNING
        with indicator.waiting("等待命令确认"):
            assert indicator.state is TerminalActivityState.WAITING
            assert titles[-1] == "☆ Xenon · 等待命令确认"
        assert indicator.state is TerminalActivityState.RUNNING
        assert titles[-1] == "✶·· Xenon"

    assert indicator.state is TerminalActivityState.IDLE
    assert titles[-1] == "✶·· Xenon"
    indicator.close()


def test_ascii_fallback_uses_fixed_width_frames():
    titles: list[str] = []
    indicator = TerminalActivityIndicator(
        enabled=True,
        ascii_only=True,
        interval=1,
        writer=titles.append,
        original_title="original",
    )

    indicator.start()
    indicator.set_state(TerminalActivityState.RUNNING)
    with indicator.waiting():
        assert titles[-1] == "- Xenon · 等待确认"
    indicator.close()

    running = [title for title in titles if title.endswith(" Xenon") and len(title) == 9]
    assert "*.. Xenon" in running


def test_title_control_characters_are_removed():
    titles: list[str] = []
    indicator = TerminalActivityIndicator(
        enabled=True,
        writer=titles.append,
        original_title="original",
    )

    indicator.set_state(TerminalActivityState.WAITING, detail="a\x1b]0;bad\x07\nnext")
    indicator.close()

    assert "\x1b" not in titles[0]
    assert "\x07" not in titles[0]
    assert "\n" not in titles[0]


def test_writer_failure_disables_animation_without_raising():
    calls = 0

    def broken_writer(title: str) -> None:
        nonlocal calls
        calls += 1
        raise OSError("terminal closed")

    indicator = TerminalActivityIndicator(enabled=True, writer=broken_writer)

    indicator.set_state(TerminalActivityState.RUNNING)
    indicator.idle()
    indicator.close()

    assert indicator.enabled is False
    assert calls == 1


def test_repl_permission_prompt_freezes_and_resumes_activity(monkeypatch):
    titles: list[str] = []
    indicator = TerminalActivityIndicator(
        enabled=True,
        interval=1,
        writer=titles.append,
        original_title="original",
    )
    repl = REPL(streaming=False)
    repl._terminal_activity = indicator
    monkeypatch.delenv("XENON_ASSUME_YES", raising=False)
    monkeypatch.setattr("xenon.repl.repl.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("xenon.repl.repl.Prompt.ask", lambda *a, **k: "y")

    with indicator.active():
        allowed, reason = repl._confirm_tool(
            "command",
            {"action": "find . -type f"},
            "CRITICAL",
        )
        assert indicator.state is TerminalActivityState.RUNNING

    assert (allowed, reason) == (True, "")
    assert "☆ Xenon · 等待命令确认" in titles
    assert titles[-1] == "✶·· Xenon"
    indicator.close()


def test_repl_main_loop_animates_submitted_work_and_restores_title(monkeypatch):
    titles: list[str] = []
    indicator = TerminalActivityIndicator(
        enabled=True,
        interval=1,
        writer=titles.append,
        original_title="shell title",
    )
    repl = REPL(streaming=False)
    repl._terminal_activity = indicator
    submitted: list[str] = []
    reads = iter(("do work", KeyboardInterrupt(), KeyboardInterrupt()))

    def fake_read():
        value = next(reads)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(repl, "_read_input", fake_read)
    monkeypatch.setattr(repl, "_handle_chat", submitted.append)
    monkeypatch.setattr(repl, "_print_welcome", lambda: None)
    monkeypatch.setattr(repl, "_check_first_run", lambda: None)
    monkeypatch.setattr(repl, "_check_auto_resume", lambda: None)
    monkeypatch.setattr(repl, "_auto_save_session", lambda: None)
    monkeypatch.setattr(repl, "_print_exit_report", lambda: None)

    repl.run()

    assert submitted == ["do work"]
    assert "✶·· Xenon" in titles
    assert "✶·· Xenon" in titles
    assert titles[-1] == "shell title"
