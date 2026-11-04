"""Real ConPTY evidence: /plan reject leaves the workspace untouched (#291).

Sibling of tty_plan_approval.py: the same real-terminal flow, but answering
``/plan reject`` instead of ``/plan approve``.  The workspace must have NO
changes afterwards (no plan_out.txt, clean git status) — the plan was
produced, shown and refused, and nothing executed.

Usage (Windows, with winpty installed):

    XENON_TTY_WORKSPACE=C:/tmp/xenon-tty-plan-reject \
    XENON_TTY_TRANSCRIPT=docs/evidence/tty-plan-reject/transcript.raw.txt \
    python scripts/tty_plan_reject.py
"""

from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import winpty
except ImportError as exc:  # pragma: no cover - Windows-only evidence tool
    raise SystemExit("winpty is required for the TTY evidence run") from exc

WORKSPACE = Path(os.environ["XENON_TTY_WORKSPACE"])
TRANSCRIPT = Path(os.environ["XENON_TTY_TRANSCRIPT"])
MODEL = os.environ.get("XENON_TTY_MODEL", "deepseek/deepseek-v4-pro")
TOKEN = os.environ.get("XENON_TTY_TOKEN", "PLAN_REJECT_8C1E44AA03D7B5F2")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _prepare_workspace() -> None:
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)
    WORKSPACE.mkdir(parents=True)
    _git(WORKSPACE, "init", "-q")
    _git(WORKSPACE, "config", "user.email", "tty@example.com")
    _git(WORKSPACE, "config", "user.name", "TTY")
    (WORKSPACE / "baseline.txt").write_text(TOKEN + "\n", encoding="utf-8")
    _git(WORKSPACE, "add", "baseline.txt")
    _git(WORKSPACE, "commit", "-q", "-m", "baseline probe")


def _clean(text: str) -> str:
    return ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def main() -> int:
    _prepare_workspace()
    env = dict(os.environ)
    env["XENON_RUNTIME"] = "kernel"
    env["PYTHONIOENCODING"] = "utf-8"
    env["XENON_SESSION_EVENTS_DIR"] = str(WORKSPACE.parent / "tty-plan-reject-session")

    process = winpty.PtyProcess.spawn(
        [shutil.which("xenon") or "xenon", "-m", MODEL],
        cwd=str(WORKSPACE),
        env=env,
        dimensions=(45, 140),
    )
    raw = bytearray()

    def pump(timeout: float = 0.0) -> None:
        deadline = time.time() + timeout
        while True:
            wait = max(0.0, min(0.2, deadline - time.time()))
            try:
                ready, _, _ = select.select([process.fileno()], [], [], wait)
            except (OSError, ValueError):
                return
            if not ready:
                if timeout <= 0 or time.time() >= deadline:
                    return
                continue
            try:
                data = process.fileobj.recv(8192)
            except Exception:
                return
            if not data:
                return
            raw.extend(data)
            if timeout <= 0:
                return

    def text() -> str:
        return bytes(raw).decode("utf-8", "replace")

    def wait_for(needle: str, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            pump(0.2)
            if needle in text():
                return True
        return False

    def send(value: str) -> None:
        process.write(value)
        pump(0.1)

    try:
        if wait_for("选择会话", 20):
            print("stage: session picker", file=sys.stderr, flush=True)
        send("0\r")
        if not wait_for("❯", 60):
            print("stage: REPL prompt not reached", file=sys.stderr, flush=True)
            raise SystemExit(2)
        print("stage: prompt ready", file=sys.stderr, flush=True)
        send("/plan on\r")
        if not wait_for("计划模式已开启", 20):
            print("stage: plan mode not enabled", file=sys.stderr, flush=True)
            raise SystemExit(3)
        print("stage: plan mode on", file=sys.stderr, flush=True)
        prompt = (
            "创建一个新文件 plan_out.txt，内容为 {token} 加一个换行；"
            "不要修改 baseline.txt，不要 push。"
        ).format(token=TOKEN)
        send(prompt + "\r")
        if not wait_for("Kernel · plan", 180):
            print("stage: planned result not reached", file=sys.stderr, flush=True)
            raise SystemExit(4)
        print("stage: planned", file=sys.stderr, flush=True)
        # Wait for the plan turn to fully settle (its ledger line) before
        # sending the decision; otherwise the keystrokes land in the tail of
        # the kernel turn's rendering and get absorbed.
        wait_for("ledger:", 30)
        pump(2)
        send("/plan reject\r")
        # Keep pumping until the REPL is back at its prompt: the rejection
        # panel (and the echoed input) lands inside this window.  The
        # workspace assertion below is the real acceptance.
        if not wait_for("驳回", 60):
            wait_for("❯", 60)
        pump(2)
        print("stage: rejected", file=sys.stderr, flush=True)
        send("/plan status\r")
        pump(3)
        send("/exit\r")
        pump(1)
    finally:
        transcript = _clean(text())
        TRANSCRIPT.parent.mkdir(parents=True, exist_ok=True)
        TRANSCRIPT.write_text(transcript, encoding="utf-8")
        try:
            process.terminate()
        except Exception:
            pass

    output = WORKSPACE / "plan_out.txt"
    print(f"transcript={TRANSCRIPT}")
    print(f"plan_out_exists={output.exists()}")
    print(f"git_status={_git(WORKSPACE, 'status', '--porcelain').stdout.strip()!r}")
    print(f"git_log={_git(WORKSPACE, 'log', '-1', '--pretty=%s').stdout.strip()!r}")
    # Acceptance: the rejected plan never executed — no new file, clean tree.
    assert not output.exists(), "a rejected plan must not create the file"
    assert not _git(WORKSPACE, "status", "--porcelain").stdout.strip(), (
        "a rejected plan must leave the workspace untouched"
    )
    print("PASS: /plan reject left the workspace untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
