"""P3-Q6 setup_wizard: ``_masked_input`` 逐字符 * 号掩码（POSIX termios 路径）。

回归用户报告的"添加 API Key 时粘贴没有反应"：旧实现 Linux 走
``getpass.getpass()``，完全关闭回显，粘贴零视觉反馈。修复后用 termios 逐字符
显示 ``*``，并在读取期间关闭 bracketed-paste，使粘贴作为普通字符流入。

通过 ``pty.openpty()`` 建立真实伪终端驱动 termios 分支（非 mock）。
"""

from __future__ import annotations

import os
import pty
import sys
import threading
import time

import pytest

from xenon.repl.setup_wizard import _masked_input


class _FakeStdin:
    """暴露 fileno() 指向 pty slave，使 _masked_input 走 termios 分支。"""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


@pytest.fixture
def pty_env(monkeypatch):
    master, slave = pty.openpty()
    monkeypatch.setattr(sys, "stdin", _FakeStdin(slave))
    monkeypatch.setattr(sys, "platform", "linux")  # 强制 POSIX 分支
    # 注意：不动 sys.stdout —— 交给 capfd 在 fd 层捕获，避免与 pytest 自带 capsys 冲突
    try:
        yield master, slave
    finally:
        for fd in (slave, master):
            try:
                os.close(fd)
            except OSError:
                pass


def _run(pty_env, capfd, chunks, timeout=3.0):
    """chunks: [(bytes, delay_after_seconds), ...]。逐段写入 master 并等待。"""
    master, slave = pty_env
    result: dict = {}

    def worker():
        try:
            result["value"] = _masked_input("请输入")
        except BaseException as exc:  # noqa: BLE001 — 透传给主线程断言
            result["exc"] = exc

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.05)  # 让 worker 进入 os.read 阻塞
    for data, delay in chunks:
        if data:
            os.write(master, data)
        if delay:
            time.sleep(delay)
    t.join(timeout)
    assert not t.is_alive(), "_masked_input 未在超时内返回（可能卡在 os.read）"
    captured = capfd.readouterr()
    return result, captured.out


# --------------------------- 基本掩码 + 粘贴反馈 ---------------------------

def test_plain_input_masks_with_stars(pty_env, capfd):
    res, out = _run(pty_env, capfd, [(b"sk-abc123\n", 0)])
    assert res["value"] == "sk-abc123"
    # 9 个字符 → 9 个 *（修复前 getpass 为 0 个 *，即"粘贴没反应"）
    assert out.count("*") == 9


def test_backspace_removes_last_char(pty_env, capfd):
    res, out = _run(pty_env, capfd, [(b"sk-ab\x7fc\n", 0)])
    assert res["value"] == "sk-ac"


def test_ctrl_u_clears_line(pty_env, capfd):
    res, out = _run(pty_env, capfd, [(b"sk-ab\x15c\n", 0)])
    # Ctrl+U 清空 "sk-ab"，再输入 "c"
    assert res["value"] == "c"


def test_escape_clears_existing_input(pty_env, capfd):
    # 输入 "sk-ab" 后按孤立 Esc（清空），再回车确认空串
    # Esc 后须等待 select 0.05s 超时，使其被判为孤立 Esc 而非转义序列
    res, out = _run(pty_env, capfd, [(b"sk-ab\x1b", 0.15), (b"\n", 0)])
    assert res["value"] == ""


def test_ctrl_c_raises_keyboard_interrupt(pty_env, capfd):
    res, out = _run(pty_env, capfd, [(b"\x03", 0)])
    assert isinstance(res.get("exc"), KeyboardInterrupt)


# --------------------------- bracketed-paste 粘贴 ---------------------------

def test_bracketed_paste_markers_stripped(pty_env, capfd):
    # prompt_toolkit REPL 开启 bracketed-paste 时，粘贴带 \e[200~..\e[201~ 标记
    # 修复后读取期间关闭该模式；即便标记漏入，转义序列消费器也会丢弃
    res, out = _run(pty_env, capfd, [(b"\x1b[200~sk-abc123\x1b[201~\n", 0)])
    assert res["value"] == "sk-abc123"
    assert out.count("*") == 9


def test_paste_with_trailing_newline_submits_cleanly(pty_env, capfd):
    # 粘贴带尾换行：换行触发确认，且无残留 \e[201~ 污染（因已关闭 bracketed-paste）
    res, out = _run(pty_env, capfd, [(b"sk-abc123\n", 0)])
    assert res["value"] == "sk-abc123"


def test_arrow_keys_ignored(pty_env, capfd):
    # 方向键 \e[C（右）等转义序列应被丢弃，不影响输入
    res, out = _run(pty_env, capfd, [(b"sk-a\x1b[Cb\n", 0)])
    assert res["value"] == "sk-ab"


# --------------------------- 非 TTY 回退 ---------------------------

def test_non_tty_falls_back_to_getpass(monkeypatch):
    # stdin 无 fileno 或 tcgetattr 失败 → 回退 getpass，不抛异常
    import getpass

    class _NoFileno:
        def fileno(self):
            raise ValueError("not a tty")

    monkeypatch.setattr(sys, "stdin", _NoFileno())
    monkeypatch.setattr(sys, "platform", "linux")
    # 函数内 lazy import getpass → patch 模块级 getpass.getpass
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "sk-fallback")
    assert _masked_input("请输入") == "sk-fallback"
