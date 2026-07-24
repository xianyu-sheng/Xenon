"""Real pseudo-terminal coverage for Xenon's interactive control paths."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import termios
import time

import pytest


pytestmark = pytest.mark.e2e
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _plain_terminal_text(value: str) -> str:
    value = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", value)
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


class _PtyProcess:
    def __init__(self, script: str, home: Path) -> None:
        master, slave = pty.openpty()
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", 42, 120, 0, 0),
        )
        env = os.environ.copy()
        env.pop("XENON_ASSUME_YES", None)
        env.update({
            "HOME": str(home),
            "TERM": "xterm-256color",
            "COLUMNS": "120",
            "LINES": "42",
            "PYTHONUNBUFFERED": "1",
        })
        self.master = master
        self.output = bytearray()
        python = shutil.which("python3") or sys.executable
        self.process = subprocess.Popen(
            [python, "-c", script],
            cwd=_REPO_ROOT,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        os.set_blocking(master, False)

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def wait_for(self, needle: str, timeout: float = 5.0) -> str:
        target = needle.encode()
        deadline = time.monotonic() + timeout
        while target not in self.output and time.monotonic() < deadline:
            readable, _, _ = select.select([self.master], [], [], 0.05)
            if readable:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    chunk = b""
                self.output.extend(chunk)
            elif self.process.poll() is not None:
                break
        rendered = self.output.decode("utf-8", errors="replace")
        assert needle in rendered, (
            f"PTY output did not contain {needle!r}; "
            f"exit={self.process.poll()}, tail={rendered[-1200:]!r}"
        )
        return rendered

    def finish(self, timeout: float = 5.0) -> str:
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            self._drain(0.05)
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=2)
            raise AssertionError("PTY child did not exit before timeout")
        self._drain(0)
        rendered = self.output.decode("utf-8", errors="replace")
        assert self.process.returncode == 0, rendered[-2000:]
        return rendered

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        try:
            os.close(self.master)
        except OSError:
            pass

    def _drain(self, timeout: float) -> None:
        readable, _, _ = select.select([self.master], [], [], timeout)
        if not readable:
            return
        while True:
            try:
                chunk = os.read(self.master, 65536)
            except (BlockingIOError, OSError):
                break
            if not chunk:
                break
            self.output.extend(chunk)


@pytest.fixture
def pty_child(tmp_path):
    children: list[_PtyProcess] = []

    def start(script: str) -> _PtyProcess:
        child = _PtyProcess(script, tmp_path)
        children.append(child)
        return child

    yield start
    for child in children:
        child.close()


@pytest.mark.parametrize(
    ("answer", "expected"),
    [(b"a\n", "RESULT=(True, '')"), (b"q\n", "RESULT=(False, '用户取消任务')")],
)
def test_permission_prompt_shows_keys_and_accepts_real_tty_input(
    pty_child,
    answer,
    expected,
):
    child = pty_child(
        "from xenon.repl.repl import REPL\n"
        "repl = REPL(streaming=False)\n"
        "result = repl._confirm_tool(\n"
        "    'command', {'action': 'find /tmp -type f'}, 'CRITICAL'\n"
        ")\n"
        "print(f'RESULT={result!r}', flush=True)\n"
    )

    prompt = _plain_terminal_text(child.wait_for("选择"))
    assert "命令: find /tmp -type f" in prompt
    assert "[y] 确认" in prompt
    assert "[n] 拒绝" in prompt
    assert "[a] 本会话允许相同操作" in prompt
    assert "[q] 取消任务" in prompt
    child.send(answer)
    output = child.finish()

    assert expected in output
    assert "Traceback" not in output


def test_ctrl_o_long_output_redraws_prompt_and_ctrl_c_remains_responsive(
    pty_child,
):
    child = pty_child(
        "from xenon.repl.repl import REPL\n"
        "repl = REPL(streaming=False)\n"
        "repl._last_mode_line = 'PTY-MODE-SENTINEL'\n"
        "repl._captured_log = '\\n'.join(\n"
        "    f'PTY-LOG-{i:03d} ' + '星' * 100 for i in range(120)\n"
        ")\n"
        "print('PTY-READY', flush=True)\n"
        "try:\n"
        "    first = repl._read_input()\n"
        "    print(f'FIRST={first}', flush=True)\n"
        "    print('SECOND-PROMPT-READY', flush=True)\n"
        "    repl._read_input()\n"
        "except KeyboardInterrupt:\n"
        "    print('CTRL-C-INTERRUPTED', flush=True)\n"
    )

    child.wait_for("PTY-READY")
    child.send(b"\x0f")  # Ctrl+O while prompt_toolkit owns the PTY.
    expanded = child.wait_for("思考过程已展开", timeout=8)
    assert "PTY-MODE-SENTINEL" in expanded
    assert "思考过程已展开" in expanded

    child.send(b"done\n")
    child.wait_for("FIRST=done")
    child.wait_for("SECOND-PROMPT-READY")
    time.sleep(0.2)
    child.send(b"\x03")  # Ctrl+C on the next real prompt.
    output = child.finish()

    assert "CTRL-C-INTERRUPTED" in output
    assert "Traceback" not in output
