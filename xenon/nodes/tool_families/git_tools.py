"""Git tool family."""

from __future__ import annotations

import logging
import shlex
import subprocess
from typing import Any

from xenon.engine.context import AgentContext
from xenon.utils.attribution import TRAILER, attribution_enabled

logger = logging.getLogger(__name__)


class GitToolsMixin:
    """Execute allow-listed Git operations through the ToolNode contract."""

    @staticmethod
    def _git_text(value: Any) -> str:
        """Normalize model-supplied git args (str or JSON list)."""
        if isinstance(value, (list, tuple)):
            return " ".join(str(item).strip() for item in value if str(item).strip())
        return str(value or "")

    @staticmethod
    def _split_git_args(text: str) -> list[str]:
        """Split git arguments without mangling Windows backslashes."""
        try:
            parts = shlex.split(text, posix=False)
        except ValueError:
            parts = text.split()
        cleaned: list[str] = []
        for part in parts:
            if len(part) >= 2 and part[0] == part[-1] and part[0] in {"'", '"'}:
                part = part[1:-1]
            if part:
                cleaned.append(part)
        return cleaned

    def _git(self, context: AgentContext) -> dict[str, Any]:
        """执行 Git 操作。支持: status, diff, log, add, commit, branch。"""
        git_cmd = self._resolve_template(
            self._git_text(self.git_command), context
        ).strip()
        extra_args = self._resolve_template(
            self._git_text(self.action), context
        ).strip()
        if git_cmd.startswith("git "):
            git_cmd = git_cmd[4:].strip()
        if extra_args.startswith("git "):
            extra_args = extra_args[4:].strip()
        # Models commonly put the actual operation in the generic ``action``
        # field while leaving ``git_command`` at its default ``status``.
        # Do not silently run a harmless status and let TaskState call it a
        # commit; promote a recognizable operation.
        known_commands = {
            "status", "diff", "diff_full", "log", "branch", "add", "stash",
            "commit", "push", "clone", "checkout", "switch", "restore",
        }
        extra_first = extra_args.split(None, 1)[0].lower() if extra_args else ""
        if git_cmd in {"", "status"} and extra_first in known_commands:
            git_cmd, extra_args = extra_args, ""

        # 安全验证
        self._validate_git_command(git_cmd)
        if extra_args:
            self._validate_git_command(extra_args)

        git_commands = {
            "status": ["git", "status", "--short"],
            "diff": ["git", "diff", "--stat"],
            "diff_full": ["git", "diff"],
            "log": ["git", "log", "--oneline", "-10"],
            "branch": ["git", "branch", "-a"],
            "add": ["git", "add", "."],
            "stash": ["git", "stash"],
        }

        if git_cmd in git_commands:
            cmd = git_commands[git_cmd]
        elif git_cmd.startswith("commit"):
            rest = git_cmd[len("commit"):].strip() or extra_args
            args = self._split_git_args(rest)
            msg = ""
            flags: list[str] = []
            i = 0
            while i < len(args):
                arg = args[i]
                if arg in {"-m", "--message"}:
                    if i + 1 < len(args):
                        msg = args[i + 1]
                        i += 2
                        continue
                    i += 1
                    continue
                if arg.startswith("--message="):
                    msg = arg.split("=", 1)[1]
                else:
                    flags.append(arg)
                i += 1
            if not msg:
                # ``rest`` may be a bare message (``commit "msg"``) rather
                # than an option list.
                if rest and not rest.startswith("-") and not flags:
                    msg = rest
                else:
                    msg = "auto commit"
            cmd = ["git", "commit", *flags]
            if attribution_enabled() and "co-authored-by: xenon" not in msg.lower():
                cmd += ["--trailer", TRAILER]
            cmd += ["-m", msg]
        elif git_cmd.startswith("add"):
            target = git_cmd[len("add"):].strip() or extra_args or "."
            targets = self._split_git_args(target) or ["."]
            cmd = ["git", "add", *targets]
        else:
            cmd = [
                "git",
                *self._split_git_args(git_cmd),
                *(self._split_git_args(extra_args) if extra_args else []),
            ]

        logger.info(f"[{self.id}] git {' '.join(cmd[1:])}")

        # v0.9.5: 默认在工作区根执行；repo_path 允许指向工作区内的具体仓库
        # （例如从 D:\ 启动、仓库在 D:\SmartBench）。路径仍走同一套围栏校验。
        cwd = self.cwd or "."
        repo_path = (
            self._resolve_template(getattr(self, "repo_path", "") or "", context) or ""
        ).strip()
        if repo_path:
            cwd = str(self._validate_path(repo_path, for_write=False))

        try:
            exec_cmd = [*self.command_prefix, *cmd] if self.command_prefix else cmd
            proc = subprocess.run(
                exec_cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=cwd,
            )
            output = proc.stdout.strip() or proc.stderr.strip()
            result = {
                "action_type": "git",
                "command": " ".join(cmd),
                "returncode": proc.returncode,
                "stdout": output,  # v0.5.3: 统一字段名，与 command 工具一致
                "output": output,  # 保留兼容
                "success": proc.returncode == 0,
            }
            # A commit's subject line is not enough to prove which files were
            # committed.  Expose the actual path set to ToolState/TaskState so
            # a commit containing only a test cannot satisfy source delivery.
            if proc.returncode == 0 and cmd[:2] == ["git", "commit"]:
                try:
                    show_cmd = [
                        *(self.command_prefix or ()),
                        "git",
                        "show",
                        "--pretty=format:",
                        "--name-only",
                        "HEAD",
                    ]
                    shown = subprocess.run(
                        show_cmd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=self.timeout,
                        cwd=cwd,
                    )
                    if shown.returncode == 0:
                        result["committed_paths"] = [
                            line.strip()
                            for line in shown.stdout.splitlines()
                            if line.strip()
                        ]
                except Exception as exc:  # noqa: BLE001 — metadata is best effort
                    logger.debug("git commit path metadata unavailable: %s", exc)
            self._write_output(context, output)
            return result
        except subprocess.TimeoutExpired:
            return {
                "action_type": "git",
                "command": " ".join(cmd),
                "returncode": -1,
                "stdout": "",
                "output": "",
                "success": False,
                "error": f"Git 命令超时 ({self.timeout}s): {' '.join(cmd)}",
            }
        except FileNotFoundError:
            return {
                "action_type": "git",
                "command": " ".join(cmd),
                "returncode": -1,
                "stdout": "",
                "output": "",
                "success": False,
                "error": "Git 未安装或不在 PATH 中。请先安装 git。",
            }
