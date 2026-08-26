"""Git tool family."""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from xenon.engine.context import AgentContext

logger = logging.getLogger(__name__)


class GitToolsMixin:
    """Execute allow-listed Git operations through the ToolNode contract."""

    def _git(self, context: AgentContext) -> dict[str, Any]:
        """执行 Git 操作。支持: status, diff, log, add, commit, branch。"""
        git_cmd = self._resolve_template(self.git_command, context).strip()
        extra_args = self._resolve_template(self.action, context).strip()

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
            msg = git_cmd.replace("commit", "").strip() or extra_args or "auto commit"
            cmd = ["git", "commit", "-m", msg]
        elif git_cmd.startswith("add"):
            target = git_cmd.replace("add", "").strip() or extra_args or "."
            cmd = ["git", "add", target]
        else:
            cmd = ["git"] + git_cmd.split() + (extra_args.split() if extra_args else [])

        logger.info(f"[{self.id}] git {' '.join(cmd[1:])}")

        try:
            exec_cmd = [*self.command_prefix, *cmd] if self.command_prefix else cmd
            proc = subprocess.run(
                exec_cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self.cwd or ".",
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
