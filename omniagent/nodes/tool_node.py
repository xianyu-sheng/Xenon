"""
ToolNode — 本地工具执行节点。

支持操作类型：
1. command — 执行终端命令（Bash/PowerShell）
2. write_file — 将内容写入文件
3. read_file — 读取文件内容
4. list_files — 目录遍历（支持 glob 模式）
5. search_files — 文件内容搜索（类似 grep）
6. git — Git 操作封装
7. web_fetch — HTTP 抓取网页内容

所有操作支持 {variable} 上下文变量替换。
"""

from __future__ import annotations

import fnmatch
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from omniagent.engine.context import AgentContext
from omniagent.nodes.base import BaseNode

logger = logging.getLogger(__name__)


class ToolNode(BaseNode):
    """本地工具执行节点，支持命令执行、文件操作、搜索、Git 和网页抓取。"""

    def __init__(
        self,
        node_id: str,
        *,
        action_type: str = "command",
        action: str = "",
        file_path: str | None = None,
        content: str | None = None,
        output_slot: str | None = None,
        cwd: str | None = None,
        timeout: int = 60,
        default_next: str | None = None,
        encoding: str = "utf-8",
        append: bool = False,
        # list_files 参数
        pattern: str = "*",
        max_depth: int = 5,
        # search_files 参数
        search_pattern: str = "",
        file_filter: str = "",
        # git 参数
        git_command: str = "status",
        # web_fetch 参数
        url: str = "",
        # edit_file 参数
        old_text: str = "",
        new_text: str = "",
    ) -> None:
        super().__init__(node_id, output_slot=output_slot, default_next=default_next)
        self.action_type = action_type
        self.action = action
        self.file_path = file_path
        self.content = content
        self.cwd = cwd
        self.timeout = timeout
        self.encoding = encoding
        self.append = append
        self.pattern = pattern
        self.max_depth = max_depth
        self.search_pattern = search_pattern
        self.file_filter = file_filter
        self.git_command = git_command
        self.url = url
        self.old_text = old_text
        self.new_text = new_text

    def execute(self, context: AgentContext) -> dict[str, Any]:
        """根据 action_type 分发到不同的处理方法。"""
        handlers = {
            "command": self._exec_command,
            "write_file": self._write_file,
            "read_file": self._read_file,
            "list_files": self._list_files,
            "search_files": self._search_files,
            "git": self._git,
            "web_fetch": self._web_fetch,
            "edit_file": self._edit_file,
        }
        handler = handlers.get(self.action_type)
        if not handler:
            raise ValueError(f"[{self.id}] 不支持的 action_type: {self.action_type}")
        return handler(context)

    # ── 命令执行 ──────────────────────────────────────────

    def _exec_command(self, context: AgentContext) -> dict[str, Any]:
        """执行终端命令。"""
        resolved_cmd = self._resolve_template(self.action, context)

        if sys.platform == "win32":
            shell_exec = ["powershell", "-Command", resolved_cmd]
        else:
            shell_exec = ["/bin/bash", "-c", resolved_cmd]

        logger.info(f"[{self.id}] 执行命令: {resolved_cmd}")

        try:
            proc = subprocess.run(
                shell_exec,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self.cwd,
            )
            result = {
                "action_type": "command",
                "command": resolved_cmd,
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "success": proc.returncode == 0,
            }
            self._write_output(context, proc.stdout.strip())
            logger.info(f"[{self.id}] 命令完成，返回码: {proc.returncode}")
            return result

        except subprocess.TimeoutExpired:
            error_msg = f"命令执行超时 ({self.timeout}s): {resolved_cmd}"
            logger.error(f"[{self.id}] {error_msg}")
            raise RuntimeError(error_msg)

    # ── 文件写入 ──────────────────────────────────────────

    def _write_file(self, context: AgentContext) -> dict[str, Any]:
        """将内容写入文件。"""
        file_path = self._resolve_template(self.file_path or "", context)
        content = self._resolve_template(self.content or "", context)

        if not file_path:
            raise ValueError(f"[{self.id}] write_file 需要 file_path")

        # 如果 content 为空，尝试从 context 中读取
        if not content and self.output_slot:
            content = context.get(self.output_slot, "")

        path = Path(file_path)
        if self.cwd:
            path = Path(self.cwd) / path

        # 创建父目录
        path.parent.mkdir(parents=True, exist_ok=True)

        mode = "a" if self.append else "w"
        logger.info(f"[{self.id}] {'追加' if self.append else '写入'}文件: {path}")

        with open(path, mode, encoding=self.encoding) as f:
            f.write(content)

        result = {
            "action_type": "write_file",
            "file_path": str(path),
            "bytes_written": len(content.encode(self.encoding)),
            "append": self.append,
            "success": True,
        }
        self._write_output(context, str(path))
        return result

    # ── 文件编辑（精确替换） ──────────────────────────────

    def _edit_file(self, context: AgentContext) -> dict[str, Any]:
        """精确文本替换编辑文件。"""
        file_path = self._resolve_template(self.file_path or "", context)
        old_text = self._resolve_template(self.old_text, context)
        new_text = self._resolve_template(self.new_text, context)

        if not file_path:
            raise ValueError(f"[{self.id}] edit_file 需要 file_path")
        if not old_text:
            raise ValueError(f"[{self.id}] edit_file 需要 old_text")

        path = Path(file_path)
        if not path.exists():
            return {"error": f"文件不存在: {path}", "success": False}

        content = path.read_text(encoding=self.encoding)
        count = content.count(old_text)

        if count == 0:
            return {"error": "未找到匹配文本", "success": False}
        if count > 1:
            return {"error": f"找到 {count} 处匹配，请提供更多上下文", "success": False}

        new_content = content.replace(old_text, new_text, 1)
        path.write_text(new_content, encoding=self.encoding)

        result = {
            "file": str(path),
            "replacements": 1,
            "success": True,
        }
        self._write_output(context, str(path))
        return result

    # ── 文件读取 ──────────────────────────────────────────

    def _read_file(self, context: AgentContext) -> dict[str, Any]:
        """读取文件内容。"""
        file_path = self._resolve_template(self.file_path or "", context)

        if not file_path:
            raise ValueError(f"[{self.id}] read_file 需要 file_path")

        path = Path(file_path)
        if self.cwd:
            path = Path(self.cwd) / path

        if not path.exists():
            result = {
                "action_type": "read_file",
                "file_path": str(path),
                "content": "",
                "exists": False,
                "success": False,
                "error": f"文件不存在: {path}",
            }
            self._write_output(context, "")
            logger.warning(f"[{self.id}] 文件不存在: {path}")
            return result

        logger.info(f"[{self.id}] 读取文件: {path}")
        content = path.read_text(encoding=self.encoding)

        result = {
            "action_type": "read_file",
            "file_path": str(path),
            "content": content,
            "size": len(content),
            "exists": True,
            "success": True,
        }
        self._write_output(context, content)
        return result

    # ── 目录遍历 ──────────────────────────────────────────

    def _list_files(self, context: AgentContext) -> dict[str, Any]:
        """遍历目录，支持 glob 模式和递归深度限制。"""
        base_path = self._resolve_template(self.file_path or ".", context)
        pattern = self._resolve_template(self.pattern, context)

        path = Path(base_path)
        if self.cwd and not path.is_absolute():
            path = Path(self.cwd) / path

        if not path.exists():
            result = {
                "action_type": "list_files", "path": str(path),
                "files": [], "count": 0, "success": False,
                "error": f"路径不存在: {path}",
            }
            self._write_output(context, f"路径不存在: {path}")
            return result

        files = []
        if path.is_file():
            files.append(str(path))
        else:
            for item in self._walk_with_depth(path, pattern, self.max_depth):
                files.append(str(item))

        display = "\n".join(files) if files else "(空目录)"
        result = {
            "action_type": "list_files", "path": str(path),
            "pattern": pattern, "files": files, "count": len(files), "success": True,
        }
        self._write_output(context, display)
        logger.info(f"[{self.id}] 列出 {len(files)} 个文件: {path}")
        return result

    def _walk_with_depth(self, base: Path, pattern: str, max_depth: int):
        """递归遍历，受深度限制。"""
        import os
        base_depth = len(base.parts)
        for root, dirs, files in os.walk(base):
            current_depth = len(Path(root).parts) - base_depth
            if current_depth > max_depth:
                dirs.clear()
                continue
            for f in files:
                if fnmatch.fnmatch(f, pattern):
                    yield Path(root) / f

    # ── 文件内容搜索 ──────────────────────────────────────

    def _search_files(self, context: AgentContext) -> dict[str, Any]:
        """在文件中搜索内容（类似 grep）。"""
        search_dir = self._resolve_template(self.file_path or ".", context)
        search_pattern = self._resolve_template(self.search_pattern, context)
        file_filter = self._resolve_template(self.file_filter, context)

        if not search_pattern:
            raise ValueError(f"[{self.id}] search_files 需要 search_pattern")

        path = Path(search_dir)
        if self.cwd and not path.is_absolute():
            path = Path(self.cwd) / path

        if not path.exists():
            result = {
                "action_type": "search_files", "path": str(path),
                "matches": [], "match_count": 0, "success": False,
                "error": f"路径不存在: {path}",
            }
            self._write_output(context, f"路径不存在: {path}")
            return result

        matches = []
        files_scanned = 0
        try:
            regex = re.compile(search_pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(search_pattern), re.IGNORECASE)

        search_files = [path] if path.is_file() else self._walk_with_depth(path, file_filter or "*", self.max_depth)

        for file_path in search_files:
            try:
                text = Path(file_path).read_text(encoding=self.encoding, errors="ignore")
                files_scanned += 1
                for i, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        matches.append({
                            "file": str(file_path), "line": i,
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

        result = {
            "action_type": "search_files", "path": str(path), "pattern": search_pattern,
            "matches": matches, "match_count": len(matches),
            "files_scanned": files_scanned, "success": True,
        }
        self._write_output(context, display)
        logger.info(f"[{self.id}] 搜索到 {len(matches)} 处匹配: {search_pattern}")
        return result

    # ── Git 操作 ──────────────────────────────────────────

    def _git(self, context: AgentContext) -> dict[str, Any]:
        """执行 Git 操作。支持: status, diff, log, add, commit, branch。"""
        git_cmd = self._resolve_template(self.git_command, context).strip()
        extra_args = self._resolve_template(self.action, context).strip()

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
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.timeout, cwd=self.cwd or ".",
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
            raise RuntimeError(f"[{self.id}] Git 命令超时: {' '.join(cmd)}")
        except FileNotFoundError:
            raise RuntimeError(f"[{self.id}] Git 未安装或不在 PATH 中")

    # ── 网页抓取 ──────────────────────────────────────────

    def _web_fetch(self, context: AgentContext) -> dict[str, Any]:
        """抓取网页内容，返回纯文本。"""
        url = self._resolve_template(self.url, context)
        if not url:
            url = self._resolve_template(self.action, context)
        if not url:
            raise ValueError(f"[{self.id}] web_fetch 需要 url")

        logger.info(f"[{self.id}] 抓取网页: {url}")

        try:
            import httpx
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                resp = client.get(url, headers={"User-Agent": "OmniAgent-CLI/0.2"})
                resp.raise_for_status()

                content_type = resp.headers.get("content-type", "")
                if "text/html" in content_type:
                    text = self._html_to_text(resp.text)
                else:
                    text = resp.text

                # 截断过长内容
                if len(text) > 50000:
                    text = text[:50000] + "\n\n... (内容已截断，超过 50000 字符)"

                result = {
                    "action_type": "web_fetch", "url": url,
                    "status_code": resp.status_code, "content": text,
                    "content_length": len(text), "success": True,
                }
                self._write_output(context, text[:5000])
                return result

        except ImportError:
            raise RuntimeError(f"[{self.id}] web_fetch 需要 httpx 库")
        except Exception as e:
            result = {
                "action_type": "web_fetch", "url": url,
                "content": "", "success": False, "error": str(e),
            }
            self._write_output(context, f"抓取失败: {e}")
            return result

    @staticmethod
    def _html_to_text(html: str) -> str:
        """简单 HTML 转纯文本。"""
        import re
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<br\s*/?\s*>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'</(p|div|h[1-6]|li|tr)>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    # ── 模板替换 ──────────────────────────────────────────

    @staticmethod
    def _resolve_template(template: str, context: AgentContext) -> str:
        import re
        def _replace(m: re.Match) -> str:
            key = m.group(1)
            val = context._store.get(key)
            return str(val) if val is not None else m.group(0)
        return re.sub(r"\{(\w+)\}", _replace, template)
