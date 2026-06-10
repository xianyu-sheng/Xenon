"""Child terminal bridge for OmniAgent REPL."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class TerminalSession:
    """Metadata for a child terminal observed by OmniAgent."""

    cwd: Path
    log_path: Path
    launched_at: datetime
    command: list[str]
    mode: str


@dataclass(frozen=True)
class TerminalLaunchResult:
    """Launch result returned by TerminalBridge."""

    success: bool
    message: str
    session: TerminalSession | None = None


class TerminalBridge:
    """Open an observable child terminal and read its transcript."""

    def __init__(self, *, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else Path.cwd() / ".omniagent" / "terminal"
        self.session: TerminalSession | None = None

    def open_terminal(self, *, cwd: str | Path | None = None) -> TerminalLaunchResult:
        """Launch a child terminal and start transcript logging."""

        workdir = Path(cwd) if cwd is not None else Path.cwd()
        workdir = workdir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        log_path = self.root / f"session-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        log_path.write_text("", encoding="utf-8")

        launch_errors: list[str] = []
        command: list[str] = []
        mode = ""
        for command, mode in self.build_launch_candidates(cwd=workdir, log_path=log_path):
            try:
                self._launch(command, mode=mode)
                break
            except Exception as exc:
                launch_errors.append(f"{mode}: {exc}")
        else:
            return TerminalLaunchResult(False, "子终端启动失败: " + " | ".join(launch_errors))

        session = TerminalSession(
            cwd=workdir,
            log_path=log_path,
            launched_at=datetime.now(),
            command=command,
            mode=mode,
        )
        self.session = session
        return TerminalLaunchResult(True, self._launch_message(session), session)

    def read_tail(self, *, lines: int = 80) -> str:
        """Read the last N lines from the active terminal transcript."""

        if not self.session:
            return "暂无子终端会话。先运行 /new_terminal。"
        if not self.session.log_path.exists():
            return f"子终端日志不存在: {self.session.log_path}"

        raw = self.session.log_path.read_text(encoding="utf-8", errors="replace")
        tail = raw.splitlines()[-max(1, lines):]
        return "\n".join(tail).strip() or "(子终端暂无输出)"

    def status(self, *, lines: int = 40) -> str:
        """Return a human-readable status report."""

        if not self.session:
            return "暂无子终端会话。先运行 /new_terminal。"
        modified = datetime.fromtimestamp(self.session.log_path.stat().st_mtime) if self.session.log_path.exists() else None
        tail = self.read_tail(lines=lines)
        return (
            f"子终端状态\n"
            f"  模式: {self.session.mode}\n"
            f"  工作目录: {self.session.cwd}\n"
            f"  日志: {self.session.log_path}\n"
            f"  启动时间: {self.session.launched_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"  最近更新: {modified.strftime('%Y-%m-%d %H:%M:%S') if modified else '未知'}\n\n"
            f"[最近输出]\n{tail}"
        )

    def build_launch_command(self, *, cwd: Path, log_path: Path) -> tuple[list[str], str]:
        """Build a platform-appropriate terminal launch command."""

        return self.build_launch_candidates(cwd=cwd, log_path=log_path)[0]

    def build_launch_candidates(self, *, cwd: Path, log_path: Path) -> list[tuple[list[str], str]]:
        """Build launch candidates, ordered from best UX to safest fallback."""

        if sys.platform == "win32":
            script = self._powershell_bootstrap(cwd=cwd, log_path=log_path)
            wt = self._windows_terminal_command()
            candidates = []
            if wt:
                candidates.append((
                    [
                        wt,
                        "-w",
                        "0",
                        "split-pane",
                        "-H",
                        "-d",
                        str(cwd),
                        "powershell",
                        "-NoExit",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                        script,
                    ],
                    "windows-terminal-split-pane",
                ))
            candidates.append((
                [
                    "powershell",
                    "-NoExit",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                "powershell-new-window",
            ))
            return candidates

        shell = os.environ.get("SHELL", "/bin/bash")
        script = (
            f"cd {sh_quote(str(cwd))}; "
            f"script -a -q {sh_quote(str(log_path))}; "
            "exec $SHELL"
        )
        return [([shell, "-lc", script], "posix-shell")]

    def _launch(self, command: list[str], *, mode: str) -> None:
        if sys.platform == "win32" and mode == "powershell-new-window":
            subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_CONSOLE)
            return
        subprocess.Popen(command)

    def _powershell_bootstrap(self, *, cwd: Path, log_path: Path) -> str:
        log = ps_quote(str(log_path))
        workdir = ps_quote(str(cwd))
        return (
            "$ErrorActionPreference = 'Continue'; "
            f"Set-Location -LiteralPath {workdir}; "
            f"Start-Transcript -Path {log} -Append | Out-Null; "
            "Write-Host 'OmniAgent child terminal is connected.' -ForegroundColor Cyan; "
            "Write-Host 'Run commands here, then use /terminal_status or /terminal_quote in OmniAgent.' -ForegroundColor DarkGray"
        )

    def _windows_terminal_command(self) -> str | None:
        found = shutil.which("wt.exe") or shutil.which("wt")
        if found:
            return found

        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            alias = Path(local_app_data) / "Microsoft" / "WindowsApps" / "wt.exe"
            try:
                if alias.exists():
                    return str(alias)
            except OSError:
                # Windows app execution aliases can be launchable even when probing
                # the reparse point reports access denied from restricted shells.
                return str(alias)

        return "wt.exe"

    def _launch_message(self, session: TerminalSession) -> str:
        pane_note = "已打开 Windows Terminal 分屏子终端。" if session.mode == "windows-terminal-split-pane" else "已打开子终端。"
        return (
            f"{pane_note}\n"
            f"工作目录: {session.cwd}\n"
            f"日志: {session.log_path}\n"
            "在子终端运行命令后，可用 /terminal_status 查看最近输出，"
            "或 /terminal_quote 将最近输出引用到 OmniAgent 上下文。"
        )


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sh_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)
