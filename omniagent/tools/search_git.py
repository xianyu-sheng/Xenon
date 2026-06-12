"""搜索与 Git 工具 — SearchFilesTool, GitTool。
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from omniagent.tools.base import BaseTool, ToolResult
from omniagent.tools.file_ops import _validate_path

logger = logging.getLogger(__name__)

_DANGEROUS_GIT_PATTERNS = [
    "push --force", "push -f", "reset --hard",
    "clean -fd", "checkout -- .", "branch -D",
    "reflog expire --all",
]


class SearchFilesTool(BaseTool):
    name = "search_files"
    description = "在本机指定目录中搜索包含关键词的文件，返回匹配的文件路径和行内容。类似 grep 功能。"
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "搜索的根目录", "default": "."},
            "search_pattern": {"type": "string", "description": "要搜索的关键词或正则表达式"},
            "file_filter": {"type": "string", "description": "文件名过滤，如 *.py（可选）"},
        },
        "required": ["search_pattern"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        search_dir = str(params.get("file_path", ".") or ".")
        search_pattern = str(params.get("search_pattern", "") or params.get("query", ""))
        file_filter = str(params.get("file_filter", "") or params.get("glob", ""))

        if not search_pattern:
            return ToolResult.schema_error("search_files 需要 search_pattern 参数")

        try:
            root = _validate_path(search_dir, for_write=False)
        except ValueError:
            root = Path(search_dir)

        if not root.exists():
            return ToolResult.error(f"路径不存在: {root}")

        try:
            regex = re.compile(search_pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(search_pattern), re.IGNORECASE)

        matches = []
        glob_pattern = file_filter or "*"
        scanned = 0

        for py_file in root.rglob(glob_pattern):
            if py_file.is_dir():
                continue
            try:
                text = py_file.read_text(encoding="utf-8", errors="ignore")
                scanned += 1
                for i, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        matches.append({
                            "file": str(py_file), "line": i,
                            "content": line.strip()[:200],
                        })
                        if len(matches) >= 200:
                            break
            except (OSError, UnicodeDecodeError):
                continue
            if len(matches) >= 200:
                break

        lines = [f"{m['file']}:{m['line']}: {m['content']}" for m in matches[:50]]
        display = "\n".join(lines) if lines else "(无匹配结果)"
        return ToolResult.ok(display, match_count=len(matches), files_scanned=scanned, matches=matches)


class GitTool(BaseTool):
    name = "git"
    description = "在本机执行 Git 版本控制操作。支持: status, diff, log, add, commit, branch。危险命令自动拦截。"
    input_schema = {
        "type": "object",
        "properties": {
            "git_command": {
                "type": "string",
                "description": "Git 子命令，如 'status'、'diff'、'log --oneline -10'、'add -A'、'commit -m msg'",
            },
        },
        "required": ["git_command"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        git_cmd = str(params.get("git_command", "")).strip()

        if not git_cmd:
            return ToolResult.schema_error("git 需要 git_command 参数")

        # 安全验证
        cmd_lower = git_cmd.lower()
        for dangerous in _DANGEROUS_GIT_PATTERNS:
            if dangerous.lower() in cmd_lower:
                return ToolResult.permission_denied(f"危险 Git 命令被拦截: '{dangerous}'")

        # 预定义快捷命令
        shortcuts: dict[str, list[str]] = {
            "status": ["git", "status", "--short"],
            "diff": ["git", "diff", "--stat"],
            "diff_full": ["git", "diff"],
            "log": ["git", "log", "--oneline", "-10"],
            "branch": ["git", "branch", "-a"],
            "add": ["git", "add", "."],
        }

        if git_cmd in shortcuts:
            cmd = shortcuts[git_cmd]
        elif git_cmd.startswith("commit"):
            msg = git_cmd.replace("commit", "").strip() or "auto commit"
            cmd = ["git", "commit", "-m", msg]
        elif git_cmd.startswith("add "):
            target = git_cmd[4:].strip()
            cmd = ["git", "add", target]
        else:
            cmd = ["git"] + git_cmd.split()

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            output = proc.stdout.strip() or proc.stderr.strip()
            return ToolResult.ok(
                output,
                returncode=proc.returncode,
                command=" ".join(cmd),
            )
        except subprocess.TimeoutExpired:
            return ToolResult.timeout("git", 30)
        except FileNotFoundError:
            return ToolResult.error("Git 未安装或不在 PATH 中")
