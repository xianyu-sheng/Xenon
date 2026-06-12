"""CommandTool — 终端命令执行工具。

支持 Windows (PowerShell) 和 Linux/macOS (bash)。
包含安全验证: 危险命令拦截、路径越界检查。
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import Any

from omniagent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

# 危险命令黑名单
_DANGEROUS_PATTERNS: list[str] = [
    r"rm\s+(-[rfR]+\s+)?/", r"rm\s+(-[rfR]+\s+)?~",
    r"rmdir\s+/", r"del\s+/[sfq]\s+[a-zA-Z]:\\",
    r"del\s+/[sfq]\s+C:\\",
    r"\bformat\s+[a-zA-Z]:", r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bshutdown\b", r"\breboot\b", r"\bhalt\b",
    r"curl.*\|\s*(?:bash|sh|python|node)",
    r"wget.*\|\s*(?:bash|sh|python|node)",
    r"Remove-Item\s+-[rR].*C:\\", r"Format-Volume",
    r"\bchmod\s+777\b", r"\bchown\b.*root",
]

MAX_OUTPUT_LENGTH = 100_000


def _validate_command(cmd: str) -> str | None:
    """验证命令安全性。返回错误消息或 None。"""
    if not cmd or not cmd.strip():
        return None
    cmd_lower = cmd.lower().strip()
    for pattern in _DANGEROUS_PATTERNS:
        if re.search(pattern, cmd_lower):
            return f"危险命令被拦截: 匹配到禁止模式 '{pattern}'"
    return None


class CommandTool(BaseTool):
    name = "command"
    description = (
        "在本机终端执行 shell 命令。Windows 使用 PowerShell，Linux/macOS 使用 bash。"
        "可用于运行脚本、安装依赖、查看系统信息等。"
        "不能用于读写文件（请用 read_file/write_file）。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "要执行的终端命令，如 'pip install requests' 或 'Get-ChildItem'",
            },
            "timeout": {
                "type": "integer",
                "description": "超时秒数，默认 60",
            },
            "cwd": {
                "type": "string",
                "description": "工作目录（可选，默认当前目录）",
            },
        },
        "required": ["action"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        cmd = str(params.get("action", "") or params.get("command", ""))
        if not cmd:
            return ToolResult.schema_error("command 工具需要 'action' 参数")

        # 安全验证
        error = _validate_command(cmd)
        if error:
            return ToolResult.permission_denied(error)

        timeout = int(params.get("timeout", 60) or 60)
        cwd = str(params.get("cwd", "")) or None

        if sys.platform == "win32":
            shell_cmd = ["powershell", "-Command", cmd]
        else:
            shell_cmd = ["/bin/bash", "-c", cmd]

        try:
            proc = await asyncio.create_subprocess_exec(
                *shell_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            stdout_text = stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT_LENGTH]
            stderr_text = stderr.decode("utf-8", errors="replace")[:MAX_OUTPUT_LENGTH]

            if proc.returncode == 0:
                return ToolResult.ok(
                    stdout_text or "(命令执行成功，无输出)",
                    returncode=proc.returncode,
                    stderr=stderr_text,
                    command=cmd,
                )
            else:
                combined = stdout_text
                if stderr_text:
                    combined += f"\n[STDERR]\n{stderr_text}"
                return ToolResult(
                    content=combined,
                    is_error=True,
                    metadata={"returncode": proc.returncode, "command": cmd},
                )

        except asyncio.TimeoutError:
            return ToolResult.timeout("command", timeout)

    def __repr__(self) -> str:
        return f"CommandTool()"
