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
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from omniagent.engine.context import AgentContext
from omniagent.nodes.base import BaseNode

logger = logging.getLogger(__name__)

# ── 安全常量 ──────────────────────────────────────────────

# 文件大小限制
MAX_READ_SIZE = 2 * 1024 * 1024       # 2MB — 读取上限
MAX_WRITE_SIZE = 10 * 1024 * 1024     # 10MB — 写入上限
MAX_VERIFY_SIZE = 1 * 1024 * 1024     # 1MB — 回读验证上限

# 系统敏感路径黑名单（写入操作禁止）
_SENSITIVE_PATHS = [
    "c:\\windows", "c:\\program files", "c:\\programdata",
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/dev", "/proc", "/sys",
    "/var/log", "/root/.ssh", "/root/.gnupg",
]

# 用户敏感目录黑名单
_USER_SENSITIVE = [
    ".ssh", ".gnupg", ".aws", ".azure", ".config/gh",
    ".docker/config.json", "credentials", "id_rsa", "id_ed25519",
]

# 危险命令黑名单模式
_DANGEROUS_CMD_PATTERNS = [
    # 删除根目录/系统目录
    r"rm\s+(-[rfR]+\s+)?/", r"rm\s+(-[rfR]+\s+)?~",
    r"rmdir\s+/", r"del\s+/[sfq]\s+[a-zA-Z]:\\",
    r"del\s+/[sfq]\s+C:\\",
    # 格式化
    r"\bformat\s+[a-zA-Z]:", r"\bmkfs\b",
    # 磁盘直接写入
    r"\bdd\s+if=",
    # 系统关机/重启
    r"\bshutdown\b", r"\breboot\b", r"\bhalt\b",
    # 下载并执行
    r"curl.*\|\s*(?:bash|sh|python|node)", r"wget.*\|\s*(?:bash|sh|python|node)",
    # PowerShell 危险命令
    r"Remove-Item\s+-[rR].*C:\\", r"Format-Volume",
    r"Clear-RecycleBin\s+-Force",
    # 权限变更
    r"\bchmod\s+777\b", r"\bchown\b.*root",
]

# 危险 Git 子命令
_DANGEROUS_GIT_PATTERNS = [
    "push --force", "push -f", "reset --hard",
    "clean -fd", "clean -fXd", "checkout -- .",
    "branch -D", "reflog expire --all",
]


class SecurityError(Exception):
    """安全策略违规异常。"""
    pass


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
        # 批量操作参数
        files: list[dict] | None = None,
        edits: list[dict] | None = None,
        # code_index / ast_analyze 参数
        symbol: str = "",
        query: str = "",
        # refactor 参数
        old_name: str = "",
        new_name: str = "",
        refactor_action: str = "rename",  # rename | clean_imports | analyze
        # diff_preview 参数
        # (复用 file_path, old_text, new_text)
        # mcp_call 参数
        tool_name: str = "",
        tool_args: dict | None = None,
        mcp_server: str = "",
        # github_fetch 参数
        repo: str = "",
        github_action: str = "list_files",  # list_files | fetch_file | fetch_readme
        github_path: str = "",
        branch: str = "main",
        # 安全参数
        security_enabled: bool = True,
        # read_file 分段读取参数
        start_line: int | None = None,
        max_lines: int | None = None,
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
        self.files = files or []
        self.edits = edits or []
        self.symbol = symbol
        self.query = query
        self.old_name = old_name
        self.new_name = new_name
        self.refactor_action = refactor_action
        self.tool_name = tool_name
        self.tool_args = tool_args or {}
        self.mcp_server = mcp_server
        self.repo = repo
        self.github_action = github_action
        self.github_path = github_path
        self.branch = branch
        self.security_enabled = security_enabled
        self._extra_start_line = start_line
        self._extra_max_lines = max_lines

    # ── 参数规范化 ──────────────────────────────────────────

    # LLM 经常使用与 ToolNode 不同的参数名，这里统一映射。
    # 注意: pattern 是 list_files 的合法参数，不能作为 search_pattern 的别名。
    _PARAM_ALIASES: dict[str, list[str]] = {
        "file_path":      ["path", "dir", "directory", "folder", "filepath", "file", "target"],
        "action":         ["command", "cmd", "shell", "exec", "run", "execute"],
        "content":        ["text", "data", "body", "value"],
        "search_pattern": ["query", "keyword", "term", "search"],
        "file_filter":    ["filter", "glob", "filetype", "ext", "extension"],
        "old_text":       ["old", "find", "search_text", "before", "original"],
        "new_text":       ["new", "replace", "replace_text", "after", "replacement"],
        "git_command":    ["subcommand", "git_cmd", "git_subcmd"],
        "url":            ["uri", "link", "href"],
        "symbol":         ["name", "func", "function_name", "class_name", "identifier"],
        "old_name":       ["from", "before_name"],
        "new_name":       ["to", "after_name"],
        "repo":           ["repository", "repo_url", "github_url", "github_repo"],
        "github_action":  ["gh_action", "git_action"],
        "github_path":    ["gh_path", "file", "filepath"],
        "branch":         ["ref", "git_branch"],
    }

    # ToolNode.__init__ 接受的所有合法参数名（不含 node_id，它是位置参数）
    _VALID_PARAMS: set[str] = {
        "action_type", "action", "file_path", "content", "output_slot",
        "cwd", "timeout", "default_next", "encoding", "append",
        "pattern", "max_depth", "search_pattern", "file_filter",
        "git_command", "url", "old_text", "new_text",
        "files", "edits", "symbol", "query",
        "old_name", "new_name", "refactor_action",
        "tool_name", "tool_args", "mcp_server",
        "repo", "github_action", "github_path", "branch",
        "security_enabled", "start_line", "max_lines",
    }

    @classmethod
    def normalize_params(cls, params: dict, *, action_type: str = "") -> dict:
        """将 LLM 常用的参数别名映射为 ToolNode 接受的标准参数名，
        并过滤掉 ToolNode 不支持的未知参数（如 LLM 凭空发明的 start_line）。

        Args:
            params: LLM 返回的原始参数字典
            action_type: 工具类型（如 "list_files"），用于跳过冲突的别名

        例: {"path": ".", "query": "foo", "start_line": 100} → {"file_path": ".", "search_pattern": "foo"}
        """
        result = dict(params)

        # 1. 别名映射
        for std_name, aliases in cls._PARAM_ALIASES.items():
            if std_name in result:
                continue  # 标准名已存在，不覆盖
            for alias in aliases:
                if alias in result:
                    result[std_name] = result.pop(alias)
                    break

        # 2. 过滤未知参数（防止 ToolNode.__init__ 因未知 kwargs 崩溃）
        filtered = {k: v for k, v in result.items() if k in cls._VALID_PARAMS}
        dropped = set(result.keys()) - set(filtered.keys())
        if dropped:
            logger.warning(f"过滤未知参数: {dropped}")
        return filtered

    # ── 安全验证 ──────────────────────────────────────────

    def _get_allowed_root(self) -> Path:
        """获取允许操作的根目录。"""
        if self.cwd:
            return Path(self.cwd).resolve()
        return Path.cwd().resolve()

    def _validate_path(self, file_path: str, *, for_write: bool = False) -> Path:
        """验证文件路径是否在安全范围内。

        Args:
            file_path: 原始文件路径
            for_write: True 表示写入操作（更严格），False 表示读取操作

        Returns:
            验证通过的 Path 对象（保留原始路径格式）

        Raises:
            SecurityError: 路径不安全
        """
        if not file_path:
            raise SecurityError("文件路径不能为空")

        path = Path(file_path)
        if self.cwd and not path.is_absolute():
            path = Path(self.cwd) / path

        # 安全检查可禁用（用于测试或受信任的调用方）
        if not self.security_enabled:
            return path

        resolved = path.resolve()
        root = self._get_allowed_root()

        # 检查路径是否在允许的根目录下
        try:
            resolved.relative_to(root)
        except ValueError:
            raise SecurityError(
                f"路径越界: {resolved} 不在允许的目录 {root} 下。"
                f"文件操作限制在项目目录内。"
            )

        # 写入操作额外检查敏感路径
        if for_write:
            resolved_lower = str(resolved).lower().replace("\\", "/")
            for sensitive in _SENSITIVE_PATHS:
                if sensitive in resolved_lower:
                    raise SecurityError(
                        f"禁止写入系统敏感路径: {resolved}"
                    )
            # 检查用户敏感文件
            name_lower = resolved.name.lower()
            for sensitive in _USER_SENSITIVE:
                if sensitive in name_lower or sensitive in resolved_lower:
                    raise SecurityError(
                        f"禁止写入敏感文件: {resolved}"
                    )

        # 返回原始路径格式（不调用 resolve，保留 Windows 短路径等）
        return path

    def _validate_command(self, cmd: str) -> None:
        """验证命令是否安全。

        Raises:
            SecurityError: 命令不安全
        """
        if not self.security_enabled:
            return
        if not cmd or not cmd.strip():
            return

        cmd_lower = cmd.lower().strip()
        for pattern in _DANGEROUS_CMD_PATTERNS:
            if re.search(pattern, cmd_lower):
                raise SecurityError(
                    f"危险命令被拦截: 匹配到禁止模式 '{pattern}'。"
                    f"命令: {cmd[:100]}"
                )

    def _validate_git_command(self, git_cmd: str) -> None:
        """验证 Git 子命令是否安全。

        Raises:
            SecurityError: Git 命令不安全
        """
        if not self.security_enabled:
            return
        cmd_lower = git_cmd.lower().strip()
        for dangerous in _DANGEROUS_GIT_PATTERNS:
            if dangerous.lower() in cmd_lower:
                raise SecurityError(
                    f"危险 Git 命令被拦截: '{dangerous}'。"
                    f"完整命令: git {git_cmd[:80]}"
                )

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
            "create_directory": self._create_directory,
            "batch_write": self._batch_write,
            "batch_edit": self._batch_edit,
            "code_index": self._code_index,
            "ast_analyze": self._ast_analyze,
            "refactor": self._refactor,
            "diff_preview": self._diff_preview,
            "mcp_call": self._mcp_call,
            "github_fetch": self._github_fetch,
        }
        handler = handlers.get(self.action_type)
        if not handler:
            raise ValueError(f"[{self.id}] 不支持的 action_type: {self.action_type}")
        return handler(context)

    # ── 命令执行 ──────────────────────────────────────────

    def _exec_command(self, context: AgentContext) -> dict[str, Any]:
        """执行终端命令。"""
        resolved_cmd = self._resolve_template(self.action, context)

        # 安全验证
        self._validate_command(resolved_cmd)

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
                encoding="utf-8",
                errors="replace",
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

        # 安全验证：路径 + 大小
        path = self._validate_path(file_path, for_write=True)
        content_bytes = len(content.encode(self.encoding))
        if content_bytes > MAX_WRITE_SIZE:
            return {
                "action_type": "write_file",
                "file_path": str(path),
                "bytes_written": 0,
                "success": False,
                "error": f"写入内容过大: {content_bytes} 字节，上限 {MAX_WRITE_SIZE} 字节",
            }

        # 创建父目录
        path.parent.mkdir(parents=True, exist_ok=True)

        mode = "a" if self.append else "w"
        logger.info(f"[{self.id}] {'追加' if self.append else '写入'}文件: {path}")

        with open(path, mode, encoding=self.encoding) as f:
            f.write(content)

        # ── 写入后验证 ──
        verify_error = self._verify_write(path, content, self.append)
        if verify_error:
            logger.error(f"[{self.id}] 写入验证失败: {verify_error}")
            return {
                "action_type": "write_file",
                "file_path": str(path),
                "bytes_written": 0,
                "success": False,
                "error": verify_error,
            }

        result = {
            "action_type": "write_file",
            "file_path": str(path),
            "bytes_written": len(content.encode(self.encoding)),
            "append": self.append,
            "success": True,
        }
        self._write_output(context, str(path))
        return result

    def _verify_write(self, path: Path, expected_content: str, is_append: bool) -> str | None:
        """验证文件写入是否成功。返回错误信息，成功返回 None。"""
        if not path.exists():
            return f"文件写入后验证失败: {path} 不存在"

        if not path.is_file():
            return f"写入验证失败: {path} 不是文件"

        # 大文件只验证存在性，不回读内容
        try:
            file_size = path.stat().st_size
        except OSError:
            return f"写入验证失败: 无法获取文件大小"

        if file_size > MAX_VERIFY_SIZE:
            logger.info(f"文件 {path} 大小 {file_size} 字节，跳过内容回读验证")
            return None

        try:
            actual = path.read_text(encoding=self.encoding)
        except UnicodeDecodeError:
            # 二进制文件无法以文本方式读取，只验证大小
            logger.info(f"文件 {path} 为二进制格式，跳过内容验证")
            return None
        except Exception as e:
            return f"写入后读取验证失败: {e}"

        if is_append:
            if not actual.endswith(expected_content) and expected_content not in actual:
                return f"追加验证失败: 写入的内容未在文件中找到"
        else:
            if actual != expected_content:
                return (
                    f"内容验证失败: 期望 {len(expected_content)} 字符, "
                    f"实际 {len(actual)} 字符"
                )

        return None

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

        # 安全验证
        path = self._validate_path(file_path, for_write=True)
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

        # ── 编辑后验证 ──
        try:
            actual = path.read_text(encoding=self.encoding)
            if actual != new_content:
                return {
                    "file": str(path),
                    "replacements": 0,
                    "success": False,
                    "error": f"编辑验证失败: 文件内容与预期不一致",
                }
        except Exception as e:
            return {
                "file": str(path),
                "replacements": 0,
                "success": False,
                "error": f"编辑后验证读取失败: {e}",
            }

        result = {
            "file": str(path),
            "replacements": 1,
            "success": True,
        }
        self._write_output(context, str(path))
        return result

    # ── 文件读取 ──────────────────────────────────────────

    def _create_directory(self, context: AgentContext) -> dict[str, Any]:
        """创建目录（含所有父目录）。"""
        dir_path = self._resolve_template(self.file_path or "", context)
        if not dir_path:
            # 兼容 action 参数
            dir_path = self._resolve_template(self.action, context)

        if not dir_path:
            raise ValueError(f"[{self.id}] create_directory 需要 file_path")

        # 安全验证
        path = self._validate_path(dir_path, for_write=True)

        logger.info(f"[{self.id}] 创建目录: {path}")

        try:
            path.mkdir(parents=True, exist_ok=True)

            # 验证目录确实存在
            if not path.exists() or not path.is_dir():
                return {
                    "action_type": "create_directory",
                    "path": str(path),
                    "success": False,
                    "error": f"目录创建后验证失败: {path} 不存在或不是目录",
                }

            result = {
                "action_type": "create_directory",
                "path": str(path),
                "success": True,
            }
            self._write_output(context, str(path))
            return result

        except Exception as e:
            return {
                "action_type": "create_directory",
                "path": str(path),
                "success": False,
                "error": f"目录创建失败: {e}",
            }

    # ── 批量操作 ──────────────────────────────────────────

    def _batch_write(self, context: AgentContext) -> dict[str, Any]:
        """批量写入多个文件。原子性：全部成功才返回成功。"""
        if not self.files:
            return {
                "action_type": "batch_write",
                "success": False,
                "error": "batch_write 需要 files 参数，格式: [{\"path\": \"...\", \"content\": \"...\"}]",
            }

        results = []
        written_paths = []

        try:
            for i, file_spec in enumerate(self.files):
                path_str = file_spec.get("path") or file_spec.get("file_path", "")
                file_content = file_spec.get("content", "")

                if not path_str:
                    results.append({"index": i, "success": False, "error": "缺少 path"})
                    continue

                # 安全验证
                path = self._validate_path(path_str, for_write=True)
                content_bytes = len(file_content.encode(self.encoding))
                if content_bytes > MAX_WRITE_SIZE:
                    results.append({
                        "index": i, "path": str(path), "success": False,
                        "error": f"内容过大: {content_bytes} 字节",
                    })
                    continue

                # 创建父目录并写入
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(file_content, encoding=self.encoding)
                written_paths.append(path)

                # 验证
                verify_error = self._verify_write(path, file_content, False)
                if verify_error:
                    results.append({
                        "index": i, "path": str(path), "success": False,
                        "error": verify_error,
                    })
                else:
                    results.append({
                        "index": i, "path": str(path), "success": True,
                        "bytes": content_bytes,
                    })

            all_ok = all(r.get("success") for r in results)
            success_count = sum(1 for r in results if r.get("success"))

            return {
                "action_type": "batch_write",
                "total": len(self.files),
                "success_count": success_count,
                "success": all_ok,
                "results": results,
            }

        except Exception as e:
            return {
                "action_type": "batch_write",
                "success": False,
                "error": f"批量写入异常: {e}",
                "results": results,
            }

    def _batch_edit(self, context: AgentContext) -> dict[str, Any]:
        """批量编辑多个文件。每个 edit 独立验证。"""
        if not self.edits:
            return {
                "action_type": "batch_edit",
                "success": False,
                "error": "batch_edit 需要 edits 参数，格式: [{\"file_path\": \"...\", \"old_text\": \"...\", \"new_text\": \"...\"}]",
            }

        results = []

        for i, edit_spec in enumerate(self.edits):
            file_path = edit_spec.get("file_path", "")
            old_text = edit_spec.get("old_text", "")
            new_text = edit_spec.get("new_text", "")

            if not file_path or not old_text:
                results.append({
                    "index": i, "success": False,
                    "error": "缺少 file_path 或 old_text",
                })
                continue

            try:
                path = self._validate_path(file_path, for_write=True)
                if not path.exists():
                    results.append({
                        "index": i, "file": str(path), "success": False,
                        "error": f"文件不存在: {path}",
                    })
                    continue

                content = path.read_text(encoding=self.encoding)
                count = content.count(old_text)

                if count == 0:
                    results.append({
                        "index": i, "file": str(path), "success": False,
                        "error": "未找到匹配文本",
                    })
                elif count > 1:
                    results.append({
                        "index": i, "file": str(path), "success": False,
                        "error": f"找到 {count} 处匹配，请提供更多上下文",
                    })
                else:
                    new_content = content.replace(old_text, new_text, 1)
                    path.write_text(new_content, encoding=self.encoding)
                    results.append({
                        "index": i, "file": str(path), "success": True,
                        "replacements": 1,
                    })

            except Exception as e:
                results.append({
                    "index": i, "success": False,
                    "error": f"编辑异常: {e}",
                })

        all_ok = all(r.get("success") for r in results)
        success_count = sum(1 for r in results if r.get("success"))

        return {
            "action_type": "batch_edit",
            "total": len(self.edits),
            "success_count": success_count,
            "success": all_ok,
            "results": results,
        }

    # ── 代码索引 / AST / 重构 ──────────────────────────────

    def _code_index(self, context: AgentContext) -> dict[str, Any]:
        """代码索引搜索。"""
        from omniagent.utils.code_index import CodeIndex

        query = self._resolve_template(self.search_pattern or self.symbol or "", context)
        file_path = self._resolve_template(self.file_path or "", context)

        if not query:
            return {
                "action_type": "code_index",
                "success": False,
                "error": "需要 search_pattern 或 symbol 参数",
            }

        # 确定索引根目录
        root = file_path if file_path and Path(file_path).is_dir() else "."
        try:
            root = str(self._validate_path(root, for_write=False))
        except Exception:
            root = "."

        index = CodeIndex(root)
        count = index.build(max_files=200)
        results = index.search(query, limit=30)
        stats = index.stats()

        matches = []
        for sym in results:
            matches.append({
                "name": sym.name,
                "kind": sym.kind,
                "file": sym.file_path,
                "line": sym.line,
                "parent": sym.parent or "",
                "signature": sym.signature,
            })

        display = f"索引 {stats['files']} 个文件, {stats['symbols']} 个符号\n"
        display += f"搜索 '{query}': 找到 {len(matches)} 个匹配\n"
        for m in matches[:20]:
            parent = f"{m['parent']}." if m['parent'] else ""
            sig = f"({m['signature']})" if m['signature'] else ""
            display += f"  {m['kind']} {parent}{m['name']}{sig} @ {m['file']}:{m['line']}\n"

        result = {
            "action_type": "code_index",
            "query": query,
            "total_files": stats["files"],
            "total_symbols": stats["symbols"],
            "matches": matches,
            "success": True,
        }
        self._write_output(context, display)
        return result

    def _ast_analyze(self, context: AgentContext) -> dict[str, Any]:
        """AST 代码分析。"""
        from omniagent.utils.ast_analyzer import ASTAnalyzer

        file_path = self._resolve_template(self.file_path or "", context)
        if not file_path:
            return {
                "action_type": "ast_analyze",
                "success": False,
                "error": "需要 file_path 参数",
            }

        path = self._validate_path(file_path, for_write=False)
        if not path.exists():
            return {
                "action_type": "ast_analyze",
                "success": False,
                "error": f"文件不存在: {path}",
            }

        analyzer = ASTAnalyzer()
        try:
            result = analyzer.analyze_file(path)
        except Exception as e:
            return {
                "action_type": "ast_analyze",
                "success": False,
                "error": f"分析失败: {e}",
            }

        display = result.summary()

        # 函数签名
        if result.functions:
            display += "\n\n函数:\n"
            for f in result.functions[:20]:
                async_kw = "async " if f.is_async else ""
                display += f"  {async_kw}def {f.name}({', '.join(f.args)}) -> {f.return_annotation or 'None'} @ 行{f.line} [复杂度:{f.complexity}]\n"

        # 类
        if result.classes:
            display += "\n\n类:\n"
            for c in result.classes[:10]:
                bases = f"({', '.join(c.bases)})" if c.bases else ""
                display += f"  class {c.name}{bases} @ 行{c.line}, {len(c.methods)} 个方法\n"

        ret = {
            "action_type": "ast_analyze",
            "file": str(path),
            "syntax_valid": result.syntax_valid,
            "functions": len(result.functions),
            "classes": len(result.classes),
            "complexity": result.complexity,
            "unused_imports": result.unused_imports,
            "success": True,
        }
        self._write_output(context, display)
        return ret

    def _refactor(self, context: AgentContext) -> dict[str, Any]:
        """代码重构操作。"""
        from omniagent.utils.refactor import RefactorEngine

        action = self._resolve_template(self.refactor_action, context)
        file_path = self._resolve_template(self.file_path or "", context)

        if not action:
            return {
                "action_type": "refactor",
                "success": False,
                "error": "需要 refactor_action 参数: rename | clean_imports | analyze",
            }

        # 确定项目根目录
        root = "."
        if file_path and Path(file_path).is_dir():
            root = str(file_path)
        elif file_path:
            root = str(Path(file_path).parent)

        try:
            root = str(self._validate_path(root, for_write=False))
        except Exception:
            root = "."

        engine = RefactorEngine(root)
        engine.build_index(max_files=200)

        if action == "rename":
            old_name = self._resolve_template(self.old_name, context)
            new_name = self._resolve_template(self.new_name, context)
            if not old_name or not new_name:
                return {
                    "action_type": "refactor",
                    "success": False,
                    "error": "rename 需要 old_name 和 new_name 参数",
                }
            result = engine.rename_symbol(old_name, new_name)
            display = f"重命名 '{old_name}' → '{new_name}'\n"
            display += f"修改 {len(result['changes'])} 处\n"
            if result["errors"]:
                display += f"错误: {'; '.join(result['errors'])}\n"
            self._write_output(context, display)
            return {"action_type": "refactor", "refactor_action": "rename", **result}

        elif action == "clean_imports":
            if not file_path:
                return {
                    "action_type": "refactor",
                    "success": False,
                    "error": "clean_imports 需要 file_path 参数",
                }
            result = engine.clean_unused_imports(file_path)
            display = f"清理导入: {file_path}\n"
            if result.get("removed"):
                display += f"移除 {len(result['removed'])} 个未使用导入\n"
            else:
                display += "没有未使用的导入\n"
            self._write_output(context, display)
            return {"action_type": "refactor", "refactor_action": "clean_imports", **result}

        elif action == "analyze":
            if not file_path:
                return {
                    "action_type": "refactor",
                    "success": False,
                    "error": "analyze 需要 file_path 参数",
                }
            result = engine.analyze_for_refactor(file_path)
            display = result["summary"]
            if result["suggestions"]:
                display += "\n\n重构建议:\n"
                for s in result["suggestions"]:
                    display += f"  [{s['type']}] {s['message']}\n"
            self._write_output(context, display)
            return {"action_type": "refactor", "refactor_action": "analyze", **result}

        else:
            return {
                "action_type": "refactor",
                "success": False,
                "error": f"未知 refactor_action: {action}。支持: rename | clean_imports | analyze",
            }

    def _diff_preview(self, context: AgentContext) -> dict[str, Any]:
        """生成 diff 预览（不实际修改文件）。"""
        import difflib

        file_path = self._resolve_template(self.file_path or "", context)
        old_text = self._resolve_template(self.old_text, context)
        new_text = self._resolve_template(self.new_text, context)

        if not file_path:
            return {
                "action_type": "diff_preview",
                "success": False,
                "error": "需要 file_path 参数",
            }

        path = self._validate_path(file_path, for_write=False)

        if old_text and new_text:
            # edit 模式：展示替换 diff
            if not path.exists():
                return {
                    "action_type": "diff_preview",
                    "success": False,
                    "error": f"文件不存在: {path}",
                }
            content = path.read_text(encoding=self.encoding)
            if old_text not in content:
                return {
                    "action_type": "diff_preview",
                    "success": False,
                    "error": "未找到匹配文本",
                }
            new_content = content.replace(old_text, new_text, 1)
        elif new_text or self.content:
            # write 模式：展示新文件 diff
            target_content = new_text or self.content or ""
            content = path.read_text(encoding=self.encoding) if path.exists() else ""
            new_content = target_content
        else:
            return {
                "action_type": "diff_preview",
                "success": False,
                "error": "需要 old_text/new_text 或 content 参数",
            }

        # 生成 diff
        old_lines = content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{Path(file_path).name}",
            tofile=f"b/{Path(file_path).name}",
            lineterm="",
        ))

        diff_text = "\n".join(diff) if diff else "(无变化)"

        result = {
            "action_type": "diff_preview",
            "file": str(path),
            "diff": diff_text,
            "has_changes": len(diff) > 0,
            "success": True,
        }
        self._write_output(context, diff_text)
        return result

    def _mcp_call(self, context: AgentContext) -> dict[str, Any]:
        """调用 MCP 服务器工具。"""
        from omniagent.mcp.registry import MCPRegistry

        tool_name = self._resolve_template(self.tool_name, context)
        if not tool_name:
            return {
                "action_type": "mcp_call",
                "success": False,
                "error": "需要 tool_name 参数",
            }

        # 获取注册表（从 context 或创建新的）
        registry = context.get("_mcp_registry")
        if not registry:
            return {
                "action_type": "mcp_call",
                "success": False,
                "error": "MCP 未初始化。请先使用 /mcp add 命令添加 MCP 服务器",
            }

        try:
            # 解析参数中的模板
            args = {}
            for k, v in self.tool_args.items():
                if isinstance(v, str):
                    args[k] = self._resolve_template(v, context)
                else:
                    args[k] = v

            result = registry.call_tool(tool_name, args)

            # 提取结果内容
            content_parts = []
            for item in result.get("content", []):
                if item.get("type") == "text":
                    content_parts.append(item.get("text", ""))
                else:
                    content_parts.append(str(item))

            display = "\n".join(content_parts) if content_parts else str(result)
            self._write_output(context, display[:5000])

            return {
                "action_type": "mcp_call",
                "tool": tool_name,
                "result": result,
                "success": True,
            }

        except Exception as e:
            return {
                "action_type": "mcp_call",
                "tool": tool_name,
                "success": False,
                "error": str(e),
            }

    def _read_file(self, context: AgentContext) -> dict[str, Any]:
        """读取文件内容。支持通过 start_line/max_lines 分段读取。"""
        file_path = self._resolve_template(self.file_path or "", context)

        if not file_path:
            raise ValueError(f"[{self.id}] read_file 需要 file_path")

        # 安全验证
        path = self._validate_path(file_path, for_write=False)

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

        # 文件大小检查
        try:
            file_size = path.stat().st_size
            if file_size > MAX_READ_SIZE:
                return {
                    "action_type": "read_file",
                    "file_path": str(path),
                    "content": "",
                    "exists": True,
                    "success": False,
                    "error": f"文件过大: {file_size} 字节，读取上限 {MAX_READ_SIZE} 字节。请使用 command + head/tail 查看部分内容。",
                }
        except OSError:
            pass

        logger.info(f"[{self.id}] 读取文件: {path}")

        # 分段读取：start_line（从 1 开始）和 max_lines
        start_line = getattr(self, '_extra_start_line', None)
        max_lines = getattr(self, '_extra_max_lines', None)

        if start_line is not None or max_lines is not None:
            # 按行分段读取
            all_lines = path.read_text(encoding=self.encoding).splitlines(keepends=True)
            total_lines = len(all_lines)
            s = max(1, int(start_line)) - 1 if start_line else 0  # 转为 0-based
            e = s + int(max_lines) if max_lines else total_lines
            e = min(e, total_lines)
            content = "".join(all_lines[s:e])
            result = {
                "action_type": "read_file",
                "file_path": str(path),
                "content": content,
                "total_lines": total_lines,
                "from_line": s + 1,
                "to_line": e,
                "size": len(content),
                "exists": True,
                "success": True,
            }
        else:
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

        # 安全验证
        path = self._validate_path(base_path, for_write=False)

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
        """递归遍历，受深度限制。支持 **/*.ext 递归 glob 模式。"""
        import os

        # 处理 **/*.ext 模式：拆分为前缀目录模式和文件名模式
        recursive_mode = "**" in pattern
        if recursive_mode:
            # "**/*.py" → file_pattern = "*.py"
            # "**/test_*.py" → file_pattern = "test_*.py"
            file_pattern = pattern.split("**/")[-1] if "**/" in pattern else pattern.replace("**", "*")
        else:
            file_pattern = pattern

        base_depth = len(base.parts)
        for root, dirs, files in os.walk(base):
            current_depth = len(Path(root).parts) - base_depth
            if not recursive_mode and current_depth > max_depth:
                dirs.clear()
                continue
            if current_depth > max_depth * 2:  # 递归模式给更多深度
                dirs.clear()
                continue
            for f in files:
                if fnmatch.fnmatch(f, file_pattern):
                    yield Path(root) / f

    # ── 文件内容搜索 ──────────────────────────────────────

    def _search_files(self, context: AgentContext) -> dict[str, Any]:
        """在文件中搜索内容（类似 grep）。"""
        search_dir = self._resolve_template(self.file_path or ".", context)
        search_pattern = self._resolve_template(self.search_pattern, context)
        file_filter = self._resolve_template(self.file_filter, context)

        if not search_pattern:
            raise ValueError(f"[{self.id}] search_files 需要 search_pattern")

        # 安全验证
        path = self._validate_path(search_dir, for_write=False)

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

        # URL 安全验证
        url_lower = url.lower().strip()
        if url_lower.startswith("file://"):
            return {
                "action_type": "web_fetch", "url": url,
                "content": "", "success": False,
                "error": "禁止访问 file:// 协议",
            }
        if any(url_lower.startswith(p) for p in ["http://169.254", "http://10.", "http://172.1", "http://192.168", "http://localhost", "http://127."]):
            return {
                "action_type": "web_fetch", "url": url,
                "content": "", "success": False,
                "error": "禁止访问内网/元数据地址",
            }

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

    def _github_fetch(self, context: AgentContext) -> dict[str, Any]:
        """GitHub 仓库操作：列出文件、获取文件内容、获取 README。

        支持的 github_action:
        - list_files: 列出仓库中所有文件路径
        - fetch_file: 获取指定文件的内容
        - fetch_readme: 获取 README 内容
        """
        repo = self._resolve_template(self.repo, context)
        if not repo:
            raise ValueError(f"[{self.id}] github_fetch 需要 repo 参数（格式: owner/repo）")

        # 规范化 repo 格式：支持完整 URL 或 owner/repo
        repo = repo.strip().rstrip("/")
        if "github.com" in repo:
            # 从 URL 提取 owner/repo
            import re
            m = re.search(r"github\.com/([^/]+/[^/]+)", repo)
            if m:
                repo = m.group(1)
        repo = repo.rstrip("/")

        action = self._resolve_template(self.github_action, context) or "list_files"
        branch = self._resolve_template(self.branch, context) or "main"
        github_path = self._resolve_template(self.github_path, context) or ""

        logger.info(f"[{self.id}] GitHub {action}: {repo} (branch={branch}, path={github_path})")

        try:
            import httpx
        except ImportError:
            raise RuntimeError(f"[{self.id}] github_fetch 需要 httpx 库")

        headers = {"User-Agent": "OmniAgent-CLI/0.2"}

        try:
            if action == "list_files":
                # 使用 GitHub API 获取文件树
                api_url = f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    resp = client.get(api_url, headers=headers)
                    if resp.status_code == 404:
                        # 尝试 master 分支
                        api_url = f"https://api.github.com/repos/{repo}/git/trees/master?recursive=1"
                        resp = client.get(api_url, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()

                tree = data.get("tree", [])
                # 只返回文件（不包括 tree 类型），过滤掉 .git 相关
                files = [
                    item["path"] for item in tree
                    if item.get("type") == "blob" and not item["path"].startswith(".git/")
                ]

                result_text = f"仓库 {repo} 共 {len(files)} 个文件:\n" + "\n".join(files)
                if len(result_text) > 10000:
                    result_text = result_text[:10000] + f"\n\n... (共 {len(files)} 个文件，已截断)"

                self._write_output(context, result_text[:5000])
                return {
                    "action_type": "github_fetch", "repo": repo,
                    "action": action, "files": files, "file_count": len(files),
                    "content": result_text, "success": True,
                }

            elif action == "fetch_file":
                if not github_path:
                    return {
                        "action_type": "github_fetch", "repo": repo,
                        "action": action, "content": "", "success": False,
                        "error": "fetch_file 需要 github_path 参数",
                    }

                raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{github_path}"
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    resp = client.get(raw_url, headers=headers)
                    if resp.status_code == 404:
                        # 尝试 master 分支
                        raw_url = f"https://raw.githubusercontent.com/{repo}/master/{github_path}"
                        resp = client.get(raw_url, headers=headers)
                    resp.raise_for_status()
                    text = resp.text

                if len(text) > 50000:
                    text = text[:50000] + "\n\n... (内容已截断，超过 50000 字符)"

                self._write_output(context, text[:5000])
                return {
                    "action_type": "github_fetch", "repo": repo,
                    "action": action, "path": github_path,
                    "content": text, "content_length": len(text), "success": True,
                }

            elif action == "fetch_readme":
                for readme_name in ["README.md", "readme.md", "README.rst", "README"]:
                    raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{readme_name}"
                    with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                        resp = client.get(raw_url, headers=headers)
                        if resp.status_code == 200:
                            text = resp.text
                            if len(text) > 20000:
                                text = text[:20000] + "\n\n... (已截断)"
                            self._write_output(context, text[:5000])
                            return {
                                "action_type": "github_fetch", "repo": repo,
                                "action": action, "path": readme_name,
                                "content": text, "success": True,
                            }

                return {
                    "action_type": "github_fetch", "repo": repo,
                    "action": action, "content": "", "success": False,
                    "error": "未找到 README 文件",
                }

            else:
                return {
                    "action_type": "github_fetch", "repo": repo,
                    "action": action, "content": "", "success": False,
                    "error": f"不支持的 github_action: {action}（可选: list_files, fetch_file, fetch_readme）",
                }

        except httpx.HTTPStatusError as e:
            return {
                "action_type": "github_fetch", "repo": repo,
                "action": action, "content": "", "success": False,
                "error": f"GitHub API 错误: {e.response.status_code} {e.response.reason_phrase}",
            }
        except Exception as e:
            return {
                "action_type": "github_fetch", "repo": repo,
                "action": action, "content": "", "success": False,
                "error": f"GitHub 操作失败: {e}",
            }

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
            val = context.get(key)
            return str(val) if val is not None else m.group(0)
        return re.sub(r"\{(\w+)\}", _replace, template)
