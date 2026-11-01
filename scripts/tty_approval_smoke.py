"""Drive a real ConPTY through the kernel approval panel.

Reproduces the report: "I answered y at the approval prompt and the run still
ended blocked".  The script types a task that requires an MCP/network call,
waits for the approval panel, answers it, and then reports what the ledger
recorded.

This is an evidence tool, not part of the runtime.  One thread on purpose: the
winpty socket deadlocks with concurrent readers/writers.

Usage (Windows, with winpty installed):

    XENON_TTY_WORKSPACE=C:/tmp/xenon-tty-approval \
    XENON_TTY_TRANSCRIPT=tmp/approval.raw.txt \
    python scripts/tty_approval_smoke.py

Environment:

- ``XENON_TTY_ANSWER``: text to type at the approval prompt (default ``y``)
- ``XENON_TTY_MODEL``: model id (default ``deepseek/deepseek-v4-flash``)
"""

from __future__ import annotations

import os
import select
import sys
import time
from pathlib import Path

try:
    import winpty
except ImportError:  # pragma: no cover - platform tool
    print("winpty is required on Windows: pip install pywinpty")
    raise SystemExit(2) from None

ANSWER = os.environ.get("XENON_TTY_ANSWER", "y")
MODEL = os.environ.get("XENON_TTY_MODEL", "deepseek/deepseek-v4-flash")
PROMPT = os.environ.get(
    "XENON_TTY_PROMPT",
    "用 github 搜索 xianyu-sheng/Xenon 这个仓库，然后告诉我它用什么语言写的",
)
TRANSCRIPT = Path(os.environ.get("XENON_TTY_TRANSCRIPT", "tmp/approval.raw.txt"))


def _drain(proc, deadline: float, sink: list[str]) -> bool:
    """Read until the approval panel shows up (or the deadline passes)."""

    seen = []
    while time.time() < deadline:
        ready, _, _ = select.select([proc.fileobj], [], [], 0.5)
        if not ready:
            continue
        try:
            chunk = proc.fileobj.recv(4096)
        except (OSError, ValueError):
            return False
        if not chunk:
            return False
        text = chunk.decode("utf-8", "replace")
        sink.append(text)
        seen.append(text)
        if "需要授权" in "".join(seen[-8:]):
            return True
    return False


def _settle(proc, sink: list[str], seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        ready, _, _ = select.select([proc.fileobj], [], [], 0.5)
        if not ready:
            continue
        try:
            chunk = proc.fileobj.recv(4096)
        except (OSError, ValueError):
            return
        if not chunk:
            return
        sink.append(chunk.decode("utf-8", "replace"))


def main() -> int:
    workspace = Path(os.environ.get("XENON_TTY_WORKSPACE", "C:/tmp/xenon-tty-approval"))
    workspace.mkdir(parents=True, exist_ok=True)
    TRANSCRIPT.parent.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("XENON_ASSUME_YES", None)

    cols, rows = 140, 45
    proc = winpty.PtyProcess.spawn(
        [sys.executable, "-m", "xenon", "chat", "-m", MODEL],
        cwd=str(workspace),
        dimensions=(rows, cols),
        env=env,
    )
    sink: list[str] = []
    try:
        _settle(proc, sink, 12.0)  # banner + skill/mcp loading
        proc.write(PROMPT + "\r")
        if not _drain(proc, time.time() + 180, sink):
            print("approval panel never appeared")
        else:
            print(f"approval panel seen; answering {ANSWER!r}")
            proc.write(ANSWER + "\r")
        _settle(proc, sink, 60.0)
        proc.write("\x03")  # leave the REPL
        _settle(proc, sink, 3.0)
    finally:
        try:
            proc.terminate(force=True)
        except Exception:  # noqa: BLE001 - teardown must not mask results
            pass

    transcript = "".join(sink)
    TRANSCRIPT.write_text(transcript, encoding="utf-8")
    print(f"transcript -> {TRANSCRIPT}")

    for marker in ("已授权本次操作", "未授权", "已取消任务", "任务未交付"):
        if marker in transcript:
            print(f"marker: {marker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
