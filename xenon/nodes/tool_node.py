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

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from xenon.engine.context import AgentContext
from xenon.nodes.base import BaseNode
from xenon.nodes.network_security import (
    MAX_REDIRECTS as _MAX_REDIRECTS,  # noqa: F401 - private compatibility export
    RFC1918_NETWORKS as _RFC1918_NETWORKS,  # noqa: F401 - private compatibility export
    SSRF_DOMAIN_ALLOWLIST as _SSRF_DOMAIN_ALLOWLIST,  # noqa: F401 - private compatibility export
    SSRFRedirectError as _SSRFRedirectError,  # noqa: F401 - private compatibility export
    SecurityError,
    fetch_with_redirect_check as _network_fetch_with_redirect_check,
    is_internal_ip as _is_internal_ip,  # noqa: F401 - private compatibility export
    is_rfc1918_private as _is_rfc1918_private,  # noqa: F401 - private compatibility export
    resolve_host_ips as _resolve_host_ips,  # noqa: F401 - private compatibility export
    ssrf_check_url as _ssrf_check_url,
)
from xenon.nodes.tool_families.code_tools import CodeToolsMixin
from xenon.nodes.tool_families.file_mutation import FileMutationToolsMixin
from xenon.nodes.tool_families.git_tools import GitToolsMixin
from xenon.nodes.tool_families.github_tools import GitHubToolsMixin
from xenon.nodes.tool_families.lsp import LSPToolsMixin
from xenon.nodes.tool_families.mcp_tools import MCPToolsMixin
from xenon.nodes.tool_families.web_tools import WebToolsMixin
from xenon.nodes.tool_families.read_only_files import (
    MAX_READ_SIZE,  # noqa: F401 - private compatibility export
    ReadOnlyFileToolsMixin,
)
from xenon.nodes.tool_families.result_filtering import (
    ResultFilteringMixin,
    _infer_time_window,  # noqa: F401 - private compatibility export
    _prefilter_keyword_context,  # noqa: F401 - private compatibility export
    _prefilter_time_records,  # noqa: F401 - private compatibility export
)
from xenon.nodes.tool_families.utility import UtilityToolsMixin
from xenon.nodes.tool_registry import (
    BUILTIN_TOOL_METHODS,
    BUILTIN_TOOL_REGISTRY,
)
from xenon.nodes.tool_result import enrich_tool_result
from xenon.utils.llm_client import (  # noqa: F401 - private compatibility export
    _create_http_client,
)

logger = logging.getLogger(__name__)


def _fetch_with_redirect_check(client, url: str, headers: dict | None = None):
    """Compatibility wrapper retaining the old patchable SSRF checker."""
    return _network_fetch_with_redirect_check(
        client, url, headers, check_url=_ssrf_check_url,
    )

# ── 动态工具注册表 ──────────────────────────────────────────
# 存储通过 register_tool 注册的自定义工具
# key: 工具名, value: {"handler": callable, "description": str, "params": dict}
_DYNAMIC_TOOLS: dict[str, dict] = {}
_GITHUB_DEFAULT_BRANCH_CACHE: dict[str, str] = {}


def register_dynamic_tool(name: str, handler, description: str, params: dict) -> None:
    """注册一个动态工具，之后可通过 ToolNode(action_type=name) 调用。"""
    _DYNAMIC_TOOLS[name] = {
        "handler": handler,
        "description": description,
        "params": params,
    }
    logger.info(f"[DynamicTool] 注册工具: {name}")


def get_dynamic_tool_schema(name: str) -> dict | None:
    """获取动态工具的描述（用于注入到 LLM 工具列表）。"""
    info = _DYNAMIC_TOOLS.get(name)
    if not info:
        return None
    return {"name": name, "description": info["description"], "params": info["params"]}


def list_dynamic_tools() -> list[str]:
    """列出所有已注册的动态工具名。"""
    return list(_DYNAMIC_TOOLS.keys())


# ── register_tool 安全策略 ──────────────────────────────
# 模式1（python_function）允许导入的模块前缀白名单。
# 默认仅允许项目自身模块；可通过环境变量 XENON_REGISTER_MODULE_ALLOW
# （逗号分隔）显式追加额外的安全模块前缀，供高级用户扩展。
_EXTRA_ALLOWED_MODULES = os.environ.get("XENON_REGISTER_MODULE_ALLOW", "")
_ALLOWED_MODULE_PREFIXES: tuple[str, ...] = ("xenon.",) + tuple(
    p.strip() + "." for p in _EXTRA_ALLOWED_MODULES.split(",") if p.strip()
)

# 危险模块顶层名：即便落在允许前缀内也一律拒绝导入（防 os.system / subprocess 等 RCE）。
_DANGEROUS_MODULE_TOPS: frozenset[str] = frozenset({
    "os", "subprocess", "builtins", "importlib", "sys", "shutil",
    "ctypes", "pickle", "socket", "ssl", "multiprocessing", "pty",
})

# 内置 action_type 集合：动态工具注册时禁止重名（防内置工具名劫持）。
# 注意：若新增内置 action_type，需同步本集合（与 ToolNode.execute 的 handlers 字典保持一致）。
_BUILTIN_ACTION_TYPES: frozenset[str] = frozenset(BUILTIN_TOOL_METHODS)


def _last_error_lines(stderr: str, max_chars: int = 300) -> str:
    """从 stderr 尾部提取错误信息。

    git 等工具把 info 行（如 "Cloning into..."）输出在前，
    真正的错误（如 "fatal: ..."）在末尾。取后 max_chars 字符。
    """
    stderr = stderr.strip()
    if len(stderr) <= max_chars:
        return stderr
    return "…" + stderr[-(max_chars - 1):]


def _validate_register_module(module_path: str) -> tuple[bool, str]:
    """校验 register_tool 模式1 的 module_path 是否在安全白名单内。

    返回 (ok, reason)；ok=False 时 reason 为人类可读的拒绝原因。
    拒绝顺序：先危险模块（os/subprocess/builtins/importlib 等），再白名单前缀。
    """
    mp = (module_path or "").strip()
    if not mp:
        return False, "module_path 为空"
    top = mp.split(".", 1)[0]
    if top in _DANGEROUS_MODULE_TOPS:
        logger.warning(f"[register_tool] 拒绝导入危险模块: {mp}")
        return False, (f"安全策略禁止导入危险模块: {top}"
                       f"（os/subprocess/builtins/importlib 等不可注册）")
    if not any(mp.startswith(p) for p in _ALLOWED_MODULE_PREFIXES):
        logger.warning(f"[register_tool] 模块不在白名单: {mp}")
        return False, (f"模块 {top} 不在注册白名单内（仅允许 xenon.*，"
                       f"或通过环境变量 XENON_REGISTER_MODULE_ALLOW 显式声明）")
    return True, ""


# ── 安全常量 ──────────────────────────────────────────────

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
    # 常见的编码/解释器混淆：黑名单不可能穷举 payload，先拒绝
    # 将外部/编码字符串交给解释器执行的高风险形态。
    r"\b(?:base64|openssl)\b[^\n|;&]*(?:-d|--decode)[^\n|;&]*\|\s*(?:bash|sh|zsh|python\w*|node)\b",
    r"\b(?:python\w*|perl|ruby|node)\s+-[ce]\s+",
    r"\b(?:bash|sh|zsh|pwsh|powershell)\s+-c\s+",
    r"\beval\s+",
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


class ToolNode(
    CodeToolsMixin,
    FileMutationToolsMixin,
    GitToolsMixin,
    GitHubToolsMixin,
    MCPToolsMixin,
    ReadOnlyFileToolsMixin,
    LSPToolsMixin,
    ResultFilteringMixin,
    UtilityToolsMixin,
    WebToolsMixin,
    BaseNode,
):
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
        limit: int | None = None,
        cursor: str | None = None,
        # search_files 参数
        search_pattern: str = "",
        file_filter: str = "",
        # git 参数
        git_command: str = "status",
        # web_fetch 参数
        url: str = "",
        start_time: str = "",
        end_time: str = "",
        # docs_fetch 参数（query 复用下方通用 query）
        max_pages: int = 4,
        max_chars: int = 12000,
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
        # github_fetch / clone_repo 参数
        repo: str = "",
        github_action: str = "list_files",  # list_files | fetch_file | fetch_readme | repo_activity
        github_path: str = "",
        branch: str = "",
        # weather 参数
        city: str = "",
        lang: str = "zh",
        # register_tool 参数
        description: str = "",
        python_function: str = "",
        command_template: str = "",
        params: dict | None = None,
        # 安全参数
        security_enabled: bool = True,
        # read_file 分段读取参数
        start_line: int | None = None,
        max_lines: int | None = None,
        # v0.6.1: LSP 工具参数
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        super().__init__(node_id, output_slot=output_slot, default_next=default_next)
        self.action_type = action_type
        self.action = action
        self.file_path = file_path
        self.content = content
        self.cwd = cwd
        # Trusted orchestration input.  It is intentionally absent from
        # _VALID_PARAMS, so model output cannot select a container or wrapper.
        self.command_prefix: tuple[str, ...] = ()
        self.command_prelude = ""
        self.timeout = timeout
        self.encoding = encoding
        self.append = append
        self.pattern = pattern
        self.max_depth = max_depth
        self.limit = limit
        self.cursor = cursor
        self.search_pattern = search_pattern
        self.file_filter = file_filter
        self.git_command = git_command
        self.url = url
        self.start_time = start_time
        self.end_time = end_time
        self.max_pages = max_pages
        self.max_chars = max_chars
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
        self.city = city
        self.lang = lang
        self.description = description
        self.python_function = python_function
        self.command_template = command_template
        self.params = params or {}
        self.security_enabled = security_enabled
        # SWE-bench 隔离容器（docker exec）内允许解释器内联验证（python -c 等）。
        # 容器边界已吸收 RCE 风险——命令在一次性沙箱中执行，不影响宿主机。
        # 宿主机（无 command_prefix）保持拦截，行为不变。
        self.allow_interpreter_inline = False
        self._extra_start_line = start_line
        self._extra_max_lines = max_lines
        # v0.6.1: LSP 工具参数
        self._lsp_line = line
        self._lsp_column = column

    def bind_command_runtime(
        self,
        prefix: tuple[str, ...],
        prelude: str = "",
    ) -> None:
        """Bind trusted command orchestration after model params are parsed."""

        self.command_prefix = tuple(prefix)
        self.command_prelude = prelude
        # docker exec 前缀 = 命令在一次性隔离容器中执行。容器边界已吸收
        # 解释器内联（python -c 等）的 RCE 风险，放行验证型命令；宿主机
        # （空前缀）保持拦截，行为不变。
        if len(prefix) >= 2 and prefix[0] == "docker" and prefix[1] == "exec":
            self.allow_interpreter_inline = True

    # ── 参数规范化 ──────────────────────────────────────────

    # LLM 经常使用与 ToolNode 不同的参数名，这里统一映射。
    # 注意: pattern 是 list_files 的合法参数，不能作为 search_pattern 的别名。
    _PARAM_ALIASES: dict[str, list[str]] = {
        "file_path":      ["path", "dir", "directory", "folder", "filepath", "file", "target", "filePath"],
        "action":         ["command", "cmd", "shell", "exec", "run", "execute"],
        "content":        ["text", "data", "body", "value", "fileContent"],
        "search_pattern": ["query", "keyword", "term", "search", "searchPattern", "queryString"],
        "file_filter":    ["filter", "glob", "filetype", "ext", "extension"],
        "old_text":       ["old", "find", "search_text", "before", "original", "oldText", "oldString", "searchString", "originalText"],
        "new_text":       ["new", "replace", "replace_text", "after", "replacement", "newText", "newString", "replacementText", "replaceString"],
        "git_command":    ["subcommand", "git_cmd", "git_subcmd"],
        "url":            ["uri", "link", "href"],
        "symbol":         ["name", "func", "function_name", "class_name", "identifier"],
        "old_name":       ["from", "before_name"],
        "new_name":       ["to", "after_name"],
        "repo":           ["repository", "repo_url", "github_url", "github_repo"],
        "github_action":  ["gh_action", "git_action"],
        "github_path":    ["gh_path", "file", "filepath"],
        "branch":         ["ref", "git_branch"],
        "city":           ["location", "place", "address"],
        "lang":           ["language", "locale"],
        # v0.6.1: LSP 参数
        "line":           ["row", "lineno", "line_number"],
        "column":         ["col", "colno", "column_number", "cursor"],
    }

    # ToolNode.__init__ 接受的所有合法参数名（不含 node_id，它是位置参数）
    _VALID_PARAMS: set[str] = {
        "action_type", "action", "file_path", "content", "output_slot",
        "cwd", "timeout", "default_next", "encoding", "append",
        "pattern", "max_depth", "search_pattern", "file_filter",
        "limit", "cursor",
        "git_command", "url", "start_time", "end_time", "old_text", "new_text",
        "max_pages", "max_chars",
        "files", "edits", "symbol", "query",
        "old_name", "new_name", "refactor_action",
        "tool_name", "tool_args", "mcp_server",
        "repo", "github_action", "github_path", "branch",
        "city", "lang", "description", "python_function", "command_template", "params",
        "security_enabled", "start_line", "max_lines",
        # v0.6.1: LSP 工具参数
        "line", "column",
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
        if "limit" not in result and "max_results" in result:
            result["limit"] = result.pop("max_results")
        if "cursor" not in result and "next_cursor" in result:
            result["cursor"] = result.pop("next_cursor")
        for std_name, aliases in cls._PARAM_ALIASES.items():
            if (
                std_name == "search_pattern"
                and action_type in {"docs_fetch", "web_fetch", "mcp_call"}
            ):
                aliases = [alias for alias in aliases if alias != "query"]
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

        resolved = Path(os.path.normpath(str(path)))
        root = self._get_allowed_root()

        # v0.5.3: 允许读写 /tmp 等临时目录
        _ALLOWED_EXTRA_ROOTS = [
            Path("/tmp").resolve(),
            Path("/var/tmp").resolve(),
        ]

        # 检查路径是否在允许的根目录下
        in_allowed_root = False
        try:
            resolved.relative_to(root)
            in_allowed_root = True
        except ValueError:
            pass

        if not in_allowed_root:
            for extra in _ALLOWED_EXTRA_ROOTS:
                try:
                    resolved.relative_to(extra)
                    in_allowed_root = True
                    break
                except ValueError:
                    pass

        if not in_allowed_root:
            raise SecurityError(
                f"路径越界: {resolved} 不在允许的目录 {root} 下。"
                f"文件操作限制在项目目录内。"
            )

        # 写入操作额外检查敏感路径
        if for_write:
            # v0.5.3: 使用路径组件匹配（加前后 /），避免 "binary" 匹配 "/bin"
            resolved_lower = str(resolved).lower().replace("\\", "/")
            # 确保路径以 / 结尾，便于组件匹配
            resolved_normalized = resolved_lower.rstrip("/") + "/"
            for sensitive in _SENSITIVE_PATHS:
                sensitive_normalized = sensitive.lower().rstrip("/") + "/"
                if resolved_normalized.startswith(sensitive_normalized) or \
                   ("/" + sensitive_normalized.lstrip("/")) in resolved_normalized:
                    raise SecurityError(
                        f"禁止写入系统敏感路径: {resolved}"
                    )
            # 检查用户敏感文件（文件名精确匹配）
            name_lower = resolved.name.lower()
            for sensitive in _USER_SENSITIVE:
                if name_lower == sensitive or name_lower.endswith(sensitive):
                    raise SecurityError(
                        f"禁止写入敏感文件: {resolved}"
                    )
        else:
            # A13: 读取操作也禁止凭证等高敏感文件，防 prompt 注入诱导泄露凭证
            name_lower = resolved.name.lower()
            resolved_lower = str(resolved).lower().replace("\\", "/")
            for sensitive in _USER_SENSITIVE:
                if sensitive in name_lower or sensitive in resolved_lower:
                    raise SecurityError(
                        f"禁止读取敏感凭证文件: {resolved}"
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
        # Shell command/process substitution executes a nested command before
        # the outer command is parsed.  A regex over the visible text cannot
        # see a payload assembled by ``$(...)``/backticks, so reject these
        # constructs at the shell boundary.  Quoted literals remain allowed.
        if self._has_unquoted_shell_substitution(cmd_lower):
            raise SecurityError(
                "命令包含未授权的 shell 命令替换（$()/反引号/进程替换），"
                "为防止混淆执行已拦截。"
            )
        # v0.3.0+ 修复（B-1）：匹配前先剥取引号内容（防止 echo "rm -rf /" 等
        # 字符串字面量触发误报）。通用机制，不针对特定任务加白名单。
        cmd_stripped = self._strip_quoted(cmd_lower)
        for pattern in _DANGEROUS_CMD_PATTERNS:
            # 隔离容器内放行解释器内联验证（python -c / python -e 等）：
            # SWE-bench 评测的标准工作流，容器边界已隔离 RCE 风险。
            # 宿主机上保持拦截。其他危险模式（rm -rf /、shutdown、base64 等）
            # 在容器内同样拦截，防止破坏评测环境。
            if (
                getattr(self, "allow_interpreter_inline", False)
                and pattern == r"\b(?:python\w*|perl|ruby|node)\s+-[ce]\s+"
            ):
                continue
            if re.search(pattern, cmd_stripped):
                raise SecurityError(
                    f"危险命令被拦截: 匹配到禁止模式 '{pattern}'。"
                    f"命令: {cmd[:100]}"
                )

    @staticmethod
    def _has_unquoted_shell_substitution(command: str) -> bool:
        """Detect shell substitution syntax outside quoted string literals."""
        quote: str | None = None
        escaped = False
        i = 0
        while i < len(command):
            char = command[i]
            if escaped:
                escaped = False
                i += 1
                continue
            if char == "\\" and quote != "'":
                escaped = True
                i += 1
                continue
            if quote:
                if char == quote:
                    quote = None
                i += 1
                continue
            if char in ("'", '"'):
                quote = char
                i += 1
                continue
            if char == "`" or command.startswith("$(", i):
                return True
            # Unquoted variable expansion can turn an innocuous-looking token
            # into a command assembled at runtime (``$cmd -rf /``).  The
            # command tool has explicit file/path parameters for safe data
            # flow, so require callers to avoid dynamic shell code here.
            if (
                char == "$"
                and i + 1 < len(command)
                and (
                    command[i + 1] == "{"
                    or re.match(
                        r"[a-z_][a-z0-9_]*", command[i + 1:], re.IGNORECASE
                    )
                )
            ):
                return True
            if char in "<>" and i + 1 < len(command) and command[i + 1] == "(":
                return True
            i += 1
        return False

    @staticmethod
    def _strip_quoted(cmd_lower: str) -> str:
        """去掉双/单引号内的内容（v0.3.0+ B-1 修复：字符串字面量不触发误报）。"""
        s = re.sub(r'"[^"]*"', '""', cmd_lower)
        s = re.sub(r"'[^']*'", "''", s)
        return s

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
        """根据注册表分发工具，保留旧的 ToolNode 调用契约。"""
        handler = BUILTIN_TOOL_REGISTRY.bind(self.action_type, self)
        if not handler:
            # 尝试从动态工具注册表中查找
            dynamic = _DYNAMIC_TOOLS.get(self.action_type)
            if dynamic:
                return enrich_tool_result(
                    self.action_type, self._exec_dynamic_tool(dynamic, context)
                )
            raise ValueError(f"[{self.id}] 不支持的 action_type: {self.action_type}")
        return enrich_tool_result(self.action_type, handler(context))

    # ── 命令执行 ──────────────────────────────────────────

    def _exec_command(self, context: AgentContext) -> dict[str, Any]:
        """执行终端命令。"""
        resolved_cmd = self._resolve_template(self.action, context)

        # 安全验证
        self._validate_command(resolved_cmd)

        shell_command = (
            f"{self.command_prelude}\n{resolved_cmd}"
            if self.command_prelude else resolved_cmd
        )
        if self.command_prefix:
            shell_exec = [*self.command_prefix, "/bin/bash", "-lc", shell_command]
        elif sys.platform == "win32":
            shell_exec = ["powershell", "-Command", resolved_cmd]
        else:
            shell_exec = ["/bin/bash", "-c", shell_command]

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
            return {
                "action_type": "command",
                "command": resolved_cmd,
                "returncode": -1,
                "stdout": "",
                "stderr": error_msg,
                "success": False,
                "error": error_msg,
            }

    # Web/document retrieval is implemented by WebToolsMixin.

    # GitHub retrieval and repository analysis are provided by GitHubToolsMixin.


    # HTML normalization is provided by WebToolsMixin.

    # ── 动态工具注册 ──────────────────────────────────────

    def _register_tool(self, context: AgentContext) -> dict[str, Any]:
        """注册一个自定义工具。支持两种模式：
        1. python_function: 指定 module_path.function_name，系统自动导入
        2. command_template: 指定命令模板，工具调用时执行 shell 命令
        """
        tool_name = self._resolve_template(getattr(self, "tool_name", ""), context)
        description = self._resolve_template(getattr(self, "description", ""), context)
        params_raw = getattr(self, "params", {})
        if isinstance(params_raw, str):
            import json
            try:
                params_raw = json.loads(params_raw)
            except json.JSONDecodeError:
                params_raw = {}

        if not tool_name:
            return {"action_type": "register_tool", "success": False, "error": "缺少 tool_name 参数"}

        # A3: 重名检查 — 禁止劫持内置 action_type，禁止覆盖已注册动态工具（除非 overwrite=True）
        overwrite = str(self._resolve_template(getattr(self, "overwrite", ""), context)).strip().lower() in (
            "1", "true", "yes", "on",
        )
        if tool_name in _BUILTIN_ACTION_TYPES:
            return {"action_type": "register_tool", "success": False,
                    "error": f"工具名 '{tool_name}' 与内置 action_type 冲突，禁止注册（防内置工具名劫持）"}
        if tool_name in _DYNAMIC_TOOLS and not overwrite:
            return {"action_type": "register_tool", "success": False,
                    "error": f"工具名 '{tool_name}' 已被注册为动态工具；如需覆盖请显式设置 overwrite=true"}

        # 模式 1: Python 函数
        python_function = self._resolve_template(getattr(self, "python_function", ""), context)
        if python_function:
            try:
                parts = python_function.rsplit(".", 1)
                if len(parts) != 2:
                    return {"action_type": "register_tool", "success": False,
                            "error": f"python_function 格式错误，应为 module.function，收到: {python_function}"}
                module_path, func_name = parts
                # A1: 模块白名单校验 — 拒绝导入 os/subprocess/builtins/importlib 等危险模块
                ok, reason = _validate_register_module(module_path)
                if not ok:
                    return {"action_type": "register_tool", "success": False, "error": reason}
                import importlib
                mod = importlib.import_module(module_path)
                func = getattr(mod, func_name)
                if not callable(func):
                    return {"action_type": "register_tool", "success": False,
                            "error": f"{python_function} 不是可调用对象"}

                def make_handler(fn):
                    def handler(ctx):
                        # 从上下文中提取参数
                        kwargs = {}
                        for key in (params_raw.get("properties") or {}):
                            val = ctx.get(key)
                            if val is not None:
                                kwargs[key] = val
                        try:
                            result = fn(**kwargs) if kwargs else fn()
                            return {"action_type": tool_name, "success": True, "content": str(result)}
                        except Exception as e:
                            return {"action_type": tool_name, "success": False, "error": str(e)}
                    return handler

                register_dynamic_tool(tool_name, make_handler(func), description or f"自定义工具: {tool_name}", params_raw)
                msg = f"✅ 工具 '{tool_name}' 注册成功（Python 函数: {python_function}）"
                logger.info(f"[register_tool] {msg}")
                return {"action_type": "register_tool", "success": True, "content": msg}

            except Exception as e:
                return {"action_type": "register_tool", "success": False, "error": f"注册失败: {e}"}

        # 模式 2: Shell 命令模板
        command_template = self._resolve_template(getattr(self, "command_template", ""), context)
        if command_template:
            def cmd_handler(ctx):
                import shlex
                cmd = command_template
                # 替换模板变量（A4: 对替换值 shlex.quote 防 shell 注入）
                for key in (params_raw.get("properties") or {}):
                    val = ctx.get(key)
                    if val is not None:
                        cmd = cmd.replace(f"{{{key}}}", shlex.quote(str(val)))
                try:
                    result = subprocess.run(
                        cmd, shell=True, capture_output=True, text=True, timeout=30
                    )
                    output = result.stdout.strip()
                    if result.returncode != 0:
                        output += f"\nSTDERR: {result.stderr.strip()}"
                    return {"action_type": tool_name, "success": result.returncode == 0,
                            "content": output, "command": cmd}
                except subprocess.TimeoutExpired:
                    return {"action_type": tool_name, "success": False, "error": "命令超时 (30s)"}
                except Exception as e:
                    return {"action_type": tool_name, "success": False, "error": str(e)}

            register_dynamic_tool(tool_name, cmd_handler, description or f"自定义命令: {tool_name}", params_raw)
            msg = f"✅ 工具 '{tool_name}' 注册成功（命令模板: {command_template}）"
            logger.info(f"[register_tool] {msg}")
            return {"action_type": "register_tool", "success": True, "content": msg}

        return {"action_type": "register_tool", "success": False,
                "error": "必须提供 python_function 或 command_template 参数"}

    def _exec_dynamic_tool(self, tool_info: dict, context: AgentContext) -> dict[str, Any]:
        """执行已注册的动态工具。"""
        handler = tool_info["handler"]
        try:
            # 将 ToolNode 的属性作为参数传给 handler
            result = handler(context)
            return result if isinstance(result, dict) else {"action_type": self.action_type, "success": True, "content": str(result)}
        except Exception as e:
            logger.error(f"[动态工具] {self.action_type} 执行失败: {e}")
            return {"action_type": self.action_type, "success": False, "error": str(e)}

    # ── 模板替换 ──────────────────────────────────────────

    @staticmethod
    def _resolve_template(template: str, context: AgentContext) -> str:
        import re
        def _replace(m: re.Match) -> str:
            key = m.group(1)
            val = context.get(key)
            return str(val) if val is not None else m.group(0)
        return re.sub(r"\{(\w+)\}", _replace, template)
