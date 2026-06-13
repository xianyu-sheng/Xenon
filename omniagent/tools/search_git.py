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
    description = (
        "在本机指定目录中搜索包含关键词的文件，返回匹配的文件路径和行内容。"
        "底层使用 ripgrep (rg) 实现高速搜索，未安装时回退到 Python re。"
        "支持正则表达式、文件类型过滤、glob 模式过滤。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "搜索的根目录", "default": "."},
            "search_pattern": {"type": "string", "description": "要搜索的关键词或正则表达式"},
            "file_filter": {"type": "string", "description": "文件名过滤，如 *.py 或 glob 模式（可选）"},
            "file_type": {"type": "string", "description": "文件类型过滤，如 py、js、rust（可选，仅 rg 模式支持）"},
        },
        "required": ["search_pattern"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        search_dir = str(params.get("file_path", ".") or ".")
        search_pattern = str(params.get("search_pattern", "") or params.get("query", ""))
        file_filter = str(params.get("file_filter", "") or params.get("glob", ""))
        file_type = str(params.get("file_type", "") or "")

        if not search_pattern:
            return ToolResult.schema_error("search_files 需要 search_pattern 参数")

        try:
            root = _validate_path(search_dir, for_write=False)
        except ValueError:
            root = Path(search_dir)

        if not root.exists():
            return ToolResult.error(f"路径不存在: {root}")

        # ── 优先使用 ripgrep ──
        rg_result = self._try_ripgrep(root, search_pattern, file_filter, file_type)
        if rg_result is not None:
            return rg_result

        # ── 回退到 Python re ──
        return self._fallback_python_re(root, search_pattern, file_filter)

    @staticmethod
    def _try_ripgrep(
        root: Path,
        pattern: str,
        file_filter: str,
        file_type: str,
    ) -> ToolResult | None:
        """尝试使用 ripgrep 搜索。返回 None 表示 rg 不可用。"""
        import shutil

        rg_path = shutil.which("rg") or shutil.which("ripgrep")
        if not rg_path:
            return None

        cmd = [
            rg_path,
            "--line-number",       # 显示行号
            "--no-heading",        # 不显示文件名标题
            "--color", "never",    # 无颜色输出
            "--max-count", "200",  # 最多 200 个匹配
            "--no-messages",       # 不显示错误消息（如权限拒绝）
            "--max-filesize", "5M",  # 只搜索 ≤5MB 的文件
        ]

        # 文件类型过滤
        if file_type:
            cmd.extend(["--type", file_type])

        # Glob 过滤
        if file_filter:
            cmd.extend(["--glob", file_filter])

        # 智能大小写：全小写关键词 → 忽略大小写
        if pattern.islower() and not any(c in pattern for c in "\\^$.*+?[]{}()|"):
            cmd.append("--ignore-case")

        cmd.extend([pattern, str(root)])

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

        if proc.returncode not in (0, 1):
            # rg 返回 1 = 无匹配，2 = 错误
            if proc.returncode == 2:
                logger.debug(f"rg 错误: {proc.stderr.strip()[:200]}")
                return None
            # 返回 1 → 无匹配
            return ToolResult.ok("(无匹配结果)", match_count=0, files_scanned=0, matches=[])

        output = proc.stdout.strip()
        if not output:
            return ToolResult.ok("(无匹配结果)", match_count=0, files_scanned=0, matches=[])

        # 解析 rg 输出: file:line:content
        matches = []
        seen_files = set()
        for line in output.split("\n"):
            # rg --no-heading 输出格式: path:line_num:content
            parts = line.split(":", 2)
            if len(parts) >= 3:
                file_path = parts[0]
                try:
                    line_num = int(parts[1])
                except ValueError:
                    continue
                content = parts[2].strip()[:200]
                matches.append({
                    "file": file_path,
                    "line": line_num,
                    "content": content,
                })
                seen_files.add(file_path)
                if len(matches) >= 200:
                    break

        lines = [f"{m['file']}:{m['line']}: {m['content']}" for m in matches[:50]]
        display = "\n".join(lines) if lines else "(无匹配结果)"
        return ToolResult.ok(
            display,
            match_count=len(matches),
            files_scanned=len(seen_files),
            matches=matches,
            engine="ripgrep",
        )

    @staticmethod
    def _fallback_python_re(
        root: Path,
        pattern: str,
        file_filter: str,
    ) -> ToolResult:
        """Python re 回退搜索（当 ripgrep 不可用时）。"""
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern), re.IGNORECASE)

        matches = []
        glob_pattern = file_filter or "*"
        scanned = 0

        for py_file in root.rglob(glob_pattern):
            if py_file.is_dir():
                continue
            # 跳过二进制/大文件 (>1MB)
            try:
                fsize = py_file.stat().st_size
                if fsize > 1_000_000:
                    continue
                if fsize == 0:
                    continue
            except OSError:
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
        return ToolResult.ok(
            display,
            match_count=len(matches),
            files_scanned=scanned,
            matches=matches,
            engine="python_re",
        )


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
