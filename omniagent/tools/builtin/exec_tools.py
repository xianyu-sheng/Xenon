"""
Execution tools — command, git
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from omniagent.engine.context import AgentContext
from omniagent.tools.builtin.base import BaseTool

logger = logging.getLogger(__name__)


class CommandTool(BaseTool):
    name = "command"

    @staticmethod
    def _normalize_cmd(cmd: str) -> str:
        """标准化命令文本：将字面换行符替换为平台对应的命令分隔符。

        LLM 有时在 JSON 字符串值中输出字面换行符而非 \\n 转义序列，
        导致命令被分割成多行。此方法在平台层面修复此问题。
        """
        if not cmd:
            return cmd
        # 将各种换行符统一替换为平台分隔符
        import re
        if sys.platform == "win32":
            # PowerShell 用 ; 分隔命令
            cmd = re.sub(r'[\r\n]+', ' ; ', cmd)
        else:
            # bash 用 ; 或 && 分隔
            cmd = re.sub(r'[\r\n]+', ' ; ', cmd)
        # 清理多余空格和连续分号
        cmd = re.sub(r'\s*;\s*;+\s*', ' ; ', cmd)
        return cmd.strip().rstrip(';').strip()

    def execute(self, context: AgentContext) -> dict[str, Any]:
        cmd = self.resolve(self._extra.get("action", ""), context)
        if not cmd:
            return {"action_type": "command", "success": False, "error": "需要 action 参数（命令文本）"}

        # P0-2 修复: 标准化命令中的换行符
        cmd = self._normalize_cmd(cmd)

        # P0-9: git clone 等长时间操作自动延长超时
        effective_timeout = self.timeout
        if "git clone" in cmd.lower() or "git clone" in cmd:
            effective_timeout = max(self.timeout, 300)
            logger.info(f"command: 检测到 git clone，超时自动延长至 {effective_timeout}s")

        self._validate_command(cmd)

        # ── 确定工作目录 ──
        work_dir = self.cwd
        if not work_dir:
            work_dir = context.get("project_root", None) or str(Path.cwd())

        if sys.platform == "win32":
            shell_exec = ["powershell", "-Command", cmd]
        else:
            shell_exec = ["/bin/bash", "-c", cmd]

        try:
            proc = subprocess.run(
                shell_exec, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=effective_timeout, cwd=work_dir,
            )
            result = {
                "action_type": "command", "command": cmd,
                "returncode": proc.returncode,
                "stdout": proc.stdout, "stderr": proc.stderr,
                "success": proc.returncode == 0,
            }
            self._write_output(context, proc.stdout.strip())
            return result
        except subprocess.TimeoutExpired:
            return {"action_type": "command", "success": False, "error": f"命令超时 ({self.timeout}s): {cmd}"}
        except FileNotFoundError:
            return {"action_type": "command", "success": False, "error": f"Shell 不可用"}


class GitTool(BaseTool):
    name = "git"

    def execute(self, context: AgentContext) -> dict[str, Any]:
        git_cmd = self.resolve(self._extra.get("git_command", "status"), context).strip()
        extra_args = self.resolve(self._extra.get("action", ""), context).strip()

        self._validate_git_command(git_cmd)

        # ── 确定工作目录 ──
        # 优先使用显式传入的 cwd，其次从 context 获取 project_root，
        # 最后回退到当前进程工作目录
        work_dir = self.cwd
        if not work_dir:
            work_dir = context.get("project_root", None) or str(Path.cwd())
        work_path = Path(work_dir)

        # ── 防御检查：确保目标目录是 Git 仓库 ──
        # 如果 cwd 不是 git 仓库，向上查找 .git 目录
        git_dir = self._find_git_dir(work_path)
        if git_dir is None:
            return {
                "action_type": "git",
                "command": f"git {git_cmd}",
                "returncode": -1,
                "output": (
                    f"当前工作目录 '{work_dir}' 及其父目录均不是 Git 仓库。\n"
                    f"请改用 list_files + read_file 来探索目录结构，"
                    f"或使用 search_files 来搜索文件内容。"
                ),
                "success": False,
                "error": f"not a git repository (cwd={work_dir})",
            }

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

        # P0-12: git clone 操作自动延长超时
        effective_timeout = self.timeout
        if "clone" in git_cmd.lower() or "clone" in (extra_args or "").lower():
            effective_timeout = max(self.timeout, 300)
            logger.info(f"git: 检测到 clone，超时延长至 {effective_timeout}s")

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=effective_timeout, cwd=str(git_dir),
            )
            output = proc.stdout.strip() or proc.stderr.strip()
            result = {
                "action_type": "git", "command": " ".join(cmd),
                "returncode": proc.returncode, "output": output,
                "success": proc.returncode == 0,
            }
            self._write_output(context, output)
            return result
        except subprocess.TimeoutExpired:
            return {"action_type": "git", "success": False, "error": "Git 命令超时"}
        except FileNotFoundError:
            return {"action_type": "git", "success": False, "error": "Git 未安装"}

    @staticmethod
    def _find_git_dir(start_path: Path) -> Path | None:
        """向上查找包含 .git 目录的根目录。

        从 start_path 开始，逐级向上查找，直到找到 .git 目录或到达文件系统根。
        返回包含 .git 的目录路径，若未找到则返回 None。
        """
        current = start_path.resolve()
        while True:
            if (current / ".git").is_dir():
                return current
            parent = current.parent
            if parent == current:  # 到达文件系统根
                return None
            current = parent
