"""Drive the installed ``xenon`` CLI through a real ConPTY.

This is the user entry, not a harness shortcut: it spawns the console script
that ``pip install -e .`` created, waits for the prompt, submits a task, and
answers the approval panels the way a person would.  Piping stdin cannot test
this path -- the built-in editor reads the terminal directly.

Usage:  python docs/evidence/repl-smoke/pty_cli_smoke.py [--keep]
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from winpty import PtyProcess

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[=>]")
# The built-in editor prints ``You: `` after a bold-cyan label; ANSI stripping
# leaves exactly this prefix.
PROMPT = "You: "
TASK = (
    "修复 calc.py 里的 bug：add 现在做的是减法。改好后运行 "
    "pytest test_calc.py 确认通过。"
)


def _strip(text: str) -> str:
    return ANSI.sub("", text).replace("\r", "")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    keep = "--keep" in sys.argv
    exe = shutil.which("xenon") or ""
    if not exe:
        print("xenon not on PATH", file=sys.stderr)
        return 2

    ws = Path(tempfile.mkdtemp(prefix="xenon-cli-smoke-"))
    (ws / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (ws / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    print("== workspace:", ws, flush=True)
    print("== entry:", exe, flush=True)

    model = os.environ.get("XENON_SMOKE_MODEL", "anthropic/deepseek-v4.1-flash")
    started = time.time()
    proc = PtyProcess.spawn(
        # ``--ui python`` keeps this probe on the built-in REPL; since
        # 2026-11-11 the bare ``xenon`` entry prefers the TS front-end when a
        # build exists, and this script asserts the Python path.
        [exe, "--ui", "python", "-m", model],
        cwd=str(ws),
        dimensions=(44, 140),
    )

    chunks: list[str] = []
    lock = threading.Lock()

    def reader() -> None:
        while True:
            try:
                data = proc.read(4096)
            except EOFError:
                break
            except Exception:  # noqa: BLE001 - stream closed
                break
            if not data:
                break
            with lock:
                chunks.append(data)

    threading.Thread(target=reader, daemon=True).start()

    def raw() -> str:
        with lock:
            return "".join(chunks)

    try:
        # The real entry first offers to resume a saved session.
        picked = False
        deadline = time.time() + 90
        while time.time() < deadline:
            screen = _strip(raw())
            if PROMPT in screen:
                break
            if "选择会话" in screen and not picked:
                print("== session picker: choosing 0 (new)", flush=True)
                proc.write("0\r")
                picked = True
            time.sleep(0.2)
        if PROMPT not in _strip(raw()):
            print("== no prompt within 90s; screen:", flush=True)
            print(_strip(raw())[-3000:], flush=True)
            return 3
        print(f"== prompt after {time.time() - started:.1f}s", flush=True)

        baseline = len(_strip(raw()))
        proc.write(TASK + "\r")
        print("== task submitted", flush=True)

        approvals = 0
        deadline = time.time() + 300
        answered = 0
        while time.time() < deadline:
            screen = _strip(raw())
            body = screen[baseline:]
            # One answer per panel.  The panel prints its help line and its
            # prompt as two separate "选择 [...]" strings, so counting those
            # sent a second, phantom "y" into the chat prompt.
            panels = body.count("需要授权")
            if panels > answered:
                answered = panels
                approvals += 1
                print(f"== approval #{approvals}: answering y", flush=True)
                proc.write("y\r")
            if "Kernel ·" in body:
                # The result panel is printed only when the turn ends.
                print(f"== turn finished (approvals answered: {approvals})", flush=True)
                break
            time.sleep(0.3)
        else:
            print("== turn did not finish within 300s", flush=True)
        # Let the redraw settle before typing the next line.
        time.sleep(2)

        print("\n===== screen (tail) =====", flush=True)
        print(_strip(raw())[-4000:], flush=True)
        print("\n== calc.py after:", flush=True)
        print((ws / "calc.py").read_text(encoding="utf-8"), flush=True)

        proc.write("/exit\r")
        time.sleep(2)
        return 0
    finally:
        try:
            proc.terminate(force=True)
        except Exception:  # noqa: BLE001 - already gone
            pass
        if not keep:
            shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
