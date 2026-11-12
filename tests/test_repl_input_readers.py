"""repl_input.py 两个输入读取器的覆盖率测试（#147）。

- ``_read_input_unix``：通过 ``pty.openpty()`` 建立真实伪终端，驱动 termios
  原始模式字节循环（非 mock），覆盖编辑键、粘贴括号模式、转义序列等分支。
- ``_read_input_windows``：在任意平台上用假的 ``ctypes.windll`` 与 ``msvcrt``
  注入按键序列驱动状态机，覆盖 Home/End/Delete、Shift+Enter 多行、
  Console API 删除 CJK 字符等 Win32 分支；真实 Win32 行为仍由
  Windows CI 上的既有用例覆盖。
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from xenon.repl.input_buffer import _ShiftTabSignal
from xenon.repl.repl_input import _read_input_unix, _read_input_windows


class _FakeRepl:
    """最小 REPL 宿主：记录输入读取器触发的回调。"""

    def __init__(self) -> None:
        self.toggled = 0
        self.shift_tabbed = 0

    def _toggle_thinking_details(self) -> None:
        self.toggled += 1

    def _handle_shift_tab(self) -> None:
        self.shift_tabbed += 1


# ─────────────────────────── Unix：真实 PTY ───────────────────────────


class _FakeStdin:
    """``fileno()`` 指向 pty slave，``encoding`` 供增量解码器使用。"""

    encoding = "utf-8"

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


@pytest.fixture
def unix_pty(monkeypatch, tmp_path):
    pty = pytest.importorskip("pty")
    try:
        master, slave = pty.openpty()
    except OSError as exc:
        pytest.skip(f"pty unavailable: {exc}")
    monkeypatch.setattr(sys, "stdin", _FakeStdin(slave))
    # The editor persists history under $HOME; keep test typing out of it.
    from xenon.repl import repl_input

    real_history = repl_input.InputHistory
    monkeypatch.setattr(
        repl_input, "InputHistory", lambda: real_history(tmp_path / "history.txt")
    )
    try:
        yield master, slave
    finally:
        for fd in (slave, master):
            try:
                os.close(fd)
            except OSError:
                pass


def _drive_unix(unix_pty, chunks, timeout=5.0):
    """``chunks``: [(bytes, delay_after_seconds), ...]，逐段写入 pty master。"""
    master, _slave = unix_pty
    repl = _FakeRepl()
    result: dict = {}

    def worker():
        try:
            result["value"] = _read_input_unix(repl)
        except BaseException as exc:  # noqa: BLE001 — 透传给主线程断言
            result["exc"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    time.sleep(0.05)  # 让 worker 进入 select / os.read 阻塞
    for data, delay in chunks:
        if data:
            os.write(master, data)
        if delay:
            time.sleep(delay)
    t.join(timeout)
    assert not t.is_alive(), "_read_input_unix 未在超时内返回（可能卡在 os.read）"
    return result, repl


def test_unix_plain_input_returns_text(unix_pty, capfd):
    res, _ = _drive_unix(unix_pty, [(b"hello\n", 0)])
    assert res["value"] == "hello"
    out = capfd.readouterr().out
    assert "You" in out
    # 进入时启用、退出时禁用粘贴括号模式
    assert "\x1b[?2004h" in out
    assert "\x1b[?2004l" in out


def test_unix_empty_line_returns_empty_string(unix_pty):
    res, _ = _drive_unix(unix_pty, [(b"\n", 0)])
    assert res["value"] == ""


def test_unix_long_line_paste_returns_full_text(unix_pty):
    # 超过 PASTE_COMPACT_THRESHOLD（1000）的粘贴走 compact/expand 路径：
    # 提交时必须还原完整文本，而不是 UI token
    text = "a" * 2000
    payload = b"\x1b[200~" + text.encode() + b"\x1b[201~\n"
    res, _ = _drive_unix(unix_pty, [(payload, 0)])
    assert res["value"] == text


def test_unix_backspace_deletes_char_before_cursor(unix_pty):
    res, _ = _drive_unix(unix_pty, [(b"ab\x7fc\n", 0)])
    assert res["value"] == "ac"


def test_unix_left_arrow_moves_cursor_for_mid_line_insert(unix_pty):
    res, _ = _drive_unix(unix_pty, [(b"ac\x1b[Db\n", 0)])
    assert res["value"] == "abc"


def test_unix_home_and_end_alternate_forms(unix_pty):
    # "\x1b[1~" = Home，"\x1b[4~" = End
    res, _ = _drive_unix(unix_pty, [(b"bc\x1b[1~a\x1b[4~d\n", 0)])
    assert res["value"] == "abcd"


def test_unix_delete_key_removes_char_under_cursor(unix_pty):
    res, _ = _drive_unix(unix_pty, [(b"abc\x1b[H\x1b[3~\n", 0)])
    assert res["value"] == "bc"


def test_unix_ctrl_d_deletes_at_cursor(unix_pty):
    res, _ = _drive_unix(unix_pty, [(b"ab\x1b[D\x04\n", 0)])
    assert res["value"] == "a"


def test_unix_ctrl_d_on_empty_line_raises_keyboard_interrupt(unix_pty):
    res, _ = _drive_unix(unix_pty, [(b"\x04", 0)])
    assert isinstance(res.get("exc"), KeyboardInterrupt)


def test_unix_ctrl_c_raises_keyboard_interrupt(unix_pty):
    res, _ = _drive_unix(unix_pty, [(b"\x03", 0)])
    assert isinstance(res.get("exc"), KeyboardInterrupt)


def test_unix_alt_enter_inserts_newline(unix_pty):
    res, _ = _drive_unix(unix_pty, [(b"line1\x1b\rline2\n", 0)])
    assert res["value"] == "line1\nline2"


def test_unix_kitty_shift_enter_inserts_newline(unix_pty):
    res, _ = _drive_unix(unix_pty, [(b"a\x1b[13;2ub\n", 0)])
    assert res["value"] == "a\nb"


def test_unix_xterm_shift_enter_inserts_newline(unix_pty):
    # "\x1b[27;2;13~" 共 10 字节：前缀守卫必须在 8 字节丢弃阈值之前生效
    res, _ = _drive_unix(unix_pty, [(b"a\x1b[27;2;13~b\n", 0)])
    assert res["value"] == "a\nb"


def test_unix_up_down_arrows_are_ignored(unix_pty):
    res, _ = _drive_unix(unix_pty, [(b"a\x1b[Ab\x1b[Bc\n", 0)])
    assert res["value"] == "abc"


def test_unix_tab_inserts_four_spaces(unix_pty):
    res, _ = _drive_unix(unix_pty, [(b"a\tb\n", 0)])
    assert res["value"] == "a    b"


def test_unix_cjk_backspace_deletes_wide_char(unix_pty):
    # 两个宽字符（各 2 列）→ 左移一位 → 退格删除"你"
    res, _ = _drive_unix(unix_pty, [("你好\x1b[D\x7f\n".encode(), 0)])
    assert res["value"] == "好"


def test_unix_bracketed_paste_single_line(unix_pty):
    res, _ = _drive_unix(unix_pty, [(b"\x1b[200~hello\x1b[201~\n", 0)])
    assert res["value"] == "hello"


def test_unix_bracketed_paste_multiline_spans_turns(unix_pty):
    res, _ = _drive_unix(unix_pty, [(b"\x1b[200~line1\nline2\x1b[201~\n", 0)])
    assert res["value"] == "line1\nline2"


def test_unix_bracketed_paste_cjk_counts_two_columns(unix_pty, capfd):
    """宽字符按 2 列计算：两个 CJK 字符的结尾光标列数应比两个 ASCII 多 2。"""

    def last_cursor_col() -> int:
        matches = re.findall(r"\r\x1b\[(\d+)C", capfd.readouterr().out)
        assert matches, "output 中未找到光标定位转义"
        return int(matches[-1])

    _drive_unix(unix_pty, [(b"\x1b[200~ab\x1b[201~\n", 0)])
    ascii_col = last_cursor_col()
    res, _ = _drive_unix(unix_pty, [("\x1b[200~你好\x1b[201~\n".encode(), 0)])
    assert res["value"] == "你好"
    assert last_cursor_col() == ascii_col + 2


def test_unix_right_arrow_moves_cursor_back(unix_pty):
    res, _ = _drive_unix(unix_pty, [(b"ac\x1b[D\x1b[Cb\n", 0)])
    assert res["value"] == "acb"


def test_unix_paste_mode_backspace_edits_paste_buffer(unix_pty):
    # 粘贴缓冲内退格删除最后一个字符，再继续粘贴
    res, _ = _drive_unix(unix_pty, [(b"\x1b[200~abc\x7fd\x1b[201~\n", 0)])
    assert res["value"] == "abd"


def test_unix_paste_mode_ctrl_c_raises_keyboard_interrupt(unix_pty):
    res, _ = _drive_unix(unix_pty, [(b"\x1b[200~abc\x03", 0)])
    assert isinstance(res.get("exc"), KeyboardInterrupt)


def test_unix_paste_mode_escaped_end_marker_inside_sequence(unix_pty):
    # 粘贴期间序列累积到 7 字节时含完整结束标记（前面多一个 ESC 字节）：
    # 子串搜索应截断标记、保留 ESC 字节，并正常退出粘贴模式
    res, _ = _drive_unix(unix_pty, [(b"\x1b[200~abc\x1b\x1b[201~\n", 0)])
    assert res["value"] == "abc\x1b"


def test_unix_paste_mode_long_sequence_flushed_to_buffer(unix_pty):
    # 粘贴期间非结束标记的 8 字节转义序列整批落入粘贴缓冲，不丢失
    res, _ = _drive_unix(
        unix_pty, [(b"\x1b[200~abc\x1b[123456", 0), (b"7\x1b[201~\n", 0)]
    )
    assert res["value"] == "abc\x1b[1234567"


def test_unix_paste_mode_recovers_when_end_marker_lost(unix_pty):
    # 缺少 \x1b[201~ 结束标记：0.3s 无新字节后自动落盘，后续回车正常提交
    res, _ = _drive_unix(unix_pty, [(b"\x1b[200~abc", 0.45), (b"\n", 0)])
    assert res["value"] == "abc"


def test_unix_without_bracketed_paste_newline_submits_per_line(unix_pty):
    """未启用粘贴括号模式时（无 \x1b[200~ 标记），每个换行独立提交一行。"""
    master, _slave = unix_pty
    repl = _FakeRepl()

    def read_after_write(data: bytes) -> str:
        result: dict = {}

        def worker():
            try:
                result["value"] = _read_input_unix(repl)
            except BaseException as exc:  # noqa: BLE001 — 透传给主线程断言
                result["exc"] = exc

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.2)  # 等 setraw（TCSAFLUSH 丢弃未读输入）完成后再写入
        os.write(master, data)
        t.join(5)
        assert not t.is_alive(), "_read_input_unix 未在超时内返回"
        assert "exc" not in result, result.get("exc")
        return result["value"]

    assert read_after_write(b"a\n") == "a"
    assert read_after_write(b"b\n") == "b"


def test_unix_unknown_escape_sequence_discarded(unix_pty):
    # 未知转义序列累积到 8 字节后被静默丢弃，不影响后续输入
    res, _ = _drive_unix(unix_pty, [(b"a\x1b[?12345\n", 0)])
    assert res["value"] == "a"


def test_unix_shift_tab_raises_signal(unix_pty):
    res, _ = _drive_unix(unix_pty, [(b"a\x1b[Z", 0)])
    assert isinstance(res.get("exc"), _ShiftTabSignal)


def test_unix_ctrl_o_toggles_thinking_details(unix_pty):
    res, repl = _drive_unix(unix_pty, [(b"x\x0f\n", 0)])
    assert res["value"] == "x"
    assert repl.toggled == 1


# ───────────────── Windows：mock ctypes.windll / msvcrt ─────────────────


class _FakeMsvcrt:
    def __init__(self, keys):
        self._keys = list(keys)

    def getwch(self) -> str:
        return self._keys.pop(0)


def _run_windows(monkeypatch, keys, shift_states=(), history_path=None):
    """注入按键序列驱动 ``_read_input_windows``，返回 (repl, 结果或异常, 调用记录)。"""
    calls: list[tuple] = []
    shift_queue = list(shift_states)

    def get_async_key_state(_vk: int) -> int:
        return shift_queue.pop(0) if shift_queue else 0

    def unused_console_api(*_args) -> bool:
        calls.append(tuple(_args))
        return True

    windll = SimpleNamespace(
        user32=SimpleNamespace(GetAsyncKeyState=get_async_key_state),
        kernel32=SimpleNamespace(
            GetStdHandle=lambda _which: 42,
            SetConsoleCursorPosition=unused_console_api,
            FillConsoleOutputCharacterW=unused_console_api,
            GetConsoleScreenBufferInfo=unused_console_api,
        ),
    )
    monkeypatch.setattr(ctypes, "windll", windll, raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt(keys))
    from xenon.repl import repl_input

    real_history = repl_input.InputHistory
    path = history_path or (Path(tempfile.mkdtemp()) / "history.txt")
    monkeypatch.setattr(repl_input, "InputHistory", lambda: real_history(path))
    repl = _FakeRepl()
    try:
        return repl, _read_input_windows(repl), calls
    except BaseException as exc:  # noqa: BLE001 — 由用例断言异常类型
        return repl, exc, calls


def test_windows_plain_input_submits(monkeypatch):
    _repl, value, _calls = _run_windows(monkeypatch, list("hello") + ["\r"])
    assert value == "hello"


def test_windows_empty_input_submits_empty_string(monkeypatch):
    _repl, value, _calls = _run_windows(monkeypatch, ["\r"])
    assert value == ""


def test_windows_long_line_returns_full_text(monkeypatch):
    text = "x" * 2000
    _repl, value, _calls = _run_windows(monkeypatch, list(text) + ["\r"])
    assert value == text


def test_windows_backspace_uses_ansi_for_ascii(monkeypatch):
    _repl, value, calls = _run_windows(monkeypatch, ["a", "b", "\x7f", "\r"])
    assert value == "a"
    assert calls == []  # ASCII 路径不调用 Console API


def test_windows_left_arrow_moves_cursor_for_mid_line_insert(monkeypatch):
    _repl, value, _calls = _run_windows(monkeypatch, ["a", "c", "\x00", "K", "b", "\r"])
    assert value == "abc"


def test_windows_home_and_delete_remove_first_char(monkeypatch):
    keys = ["a", "b", "c", "\x00", "G", "\x00", "S", "\r"]
    _repl, value, _calls = _run_windows(monkeypatch, keys)
    assert value == "bc"


def test_windows_up_arrow_recalls_previous_input(monkeypatch, tmp_path):
    path = tmp_path / "history.txt"
    path.write_text("earlier\n", encoding="utf-8")

    _repl, value, _calls = _run_windows(
        monkeypatch, ["\x00", "H", "\r"], history_path=path
    )

    assert value == "earlier"


def test_windows_end_key_moves_cursor_to_line_end(monkeypatch):
    # 光标在行中时按 End：跳到行尾后继续输入
    keys = ["a", "b", "\x00", "K", "\x00", "O", "c", "\r"]
    _repl, value, _calls = _run_windows(monkeypatch, keys)
    assert value == "abc"


def test_windows_right_arrow_moves_cursor_back(monkeypatch):
    keys = ["a", "c", "\x00", "K", "\x00", "M", "b", "\r"]
    _repl, value, _calls = _run_windows(monkeypatch, keys)
    assert value == "acb"


def test_windows_shift_enter_starts_new_line(monkeypatch):
    _repl, value, _calls = _run_windows(
        monkeypatch, ["a", "\r", "b", "\r"], shift_states=[0x8000, 0]
    )
    assert value == "a\nb"


def test_windows_shift_enter_from_mid_line_appends_tail(monkeypatch):
    # 光标在行中时 Shift+Enter：先把光标跳到行尾再换行，避免残留
    keys = ["a", "c", "\x00", "K", "\r", "b", "\r"]
    _repl, value, _calls = _run_windows(monkeypatch, keys, shift_states=[0x8000, 0])
    assert value == "ac\nb"


def test_windows_ctrl_c_raises_keyboard_interrupt(monkeypatch):
    _repl, value, _calls = _run_windows(monkeypatch, ["\x03"])
    assert isinstance(value, KeyboardInterrupt)


def test_windows_shift_tab_calls_paradigm_switch(monkeypatch):
    repl, value, _calls = _run_windows(monkeypatch, ["\x00", "\x0f", "x", "\r"])
    assert value == "x"
    assert repl.shift_tabbed == 1


def test_windows_backspace_deletes_cjk_char(monkeypatch):
    _repl, value, calls = _run_windows(monkeypatch, ["你", "好", "\x7f", "\r"])
    assert value == "你"
    assert calls == []  # 退格走 ANSI 路径，不调用 Console API
