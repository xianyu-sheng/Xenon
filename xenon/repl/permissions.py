"""
v0.5.0: 权限门控系统 — 危险工具操作确认。

提供类似 Claude Code permissionMode 的工具执行确认机制：
- DEFAULT: 危险操作弹框确认
- ACCEPT_EDITS: 自动批准编辑类操作，shell 仍需确认
- BYPASS: 跳过所有确认（CI/自动化场景）
- PLAN: 只读模式，拒绝所有写入操作
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Callable

from rich.markup import escape


class PermissionMode(Enum):
    """权限模式。"""
    DEFAULT = "default"           # 危险操作确认
    ACCEPT_EDITS = "accept_edits" # 自动批准编辑
    BYPASS = "bypass"            # 跳过确认
    PLAN = "plan"                # 只读模式


# ── 工具分类 ────────────────────────────────────────────

_CRITICAL_TOOLS: frozenset[str] = frozenset({
    "command",
    # MCP servers may expose arbitrary external side effects.  Until a server
    # advertises a trustworthy read/write schema, require an explicit approval.
    "mcp_call",
})

_WRITE_TOOLS: frozenset[str] = frozenset({
    "write_file", "edit_file", "create_directory",
    "batch_write", "batch_edit", "edit_with_llm", "append_file",
    "refactor", "register_tool", "clone_repo",
})

# git 命令中的危险子命令
_DANGEROUS_GIT_COMMANDS: frozenset[str] = frozenset({
    "push", "reset", "clean", "checkout", "restore", "rebase", "merge",
    "pull", "remote", "config", "branch -d", "branch -D", "tag -d",
})


class PermissionGate:
    """工具执行权限门控。

    在工具执行前检查是否需要用户确认，
    支持会话级别的"总是允许"记忆。
    """

    def __init__(self, mode: PermissionMode = PermissionMode.DEFAULT) -> None:
        self.mode = mode
        # 会话级别记忆：用户选择"总是允许"的工具名集合
        self._session_allow: set[str] = set()
        # CRITICAL 工具不能按工具名整体放行；只记忆参数完全相同的操作。
        self._session_allow_exact: set[str] = set()
        # 外部确认回调：签名 (tool_name, params, risk_level) -> bool
        self._confirm_callback: Callable[[str, dict, str], bool] | None = None

    def set_confirm_callback(self, cb: Callable[[str, dict, str], bool]) -> None:
        """设置确认回调（由 REPL 层注入）。"""
        self._confirm_callback = cb

    def set_mode(self, mode: PermissionMode) -> None:
        """切换权限模式。"""
        self.mode = mode

    def _classify(self, tool_name: str, params: dict | None = None) -> str:
        """分类工具风险级别。"""
        if tool_name in _CRITICAL_TOOLS:
            return "CRITICAL"
        if tool_name in _WRITE_TOOLS:
            return "WRITE"
        if tool_name == "git":
            # git 工具的子命令决定风险
            git_command = str(
                (params or {}).get("git_command")
                or (params or {}).get("action")
                or ""
            ).strip().lower()
            if any(d.lower() in git_command for d in _DANGEROUS_GIT_COMMANDS):
                return "CRITICAL"
            if git_command in {"status", "diff", "diff_full", "log", "branch", "show"}:
                return "READ"
            return "WRITE"
        return "READ"

    def check(
        self,
        tool_name: str,
        params: dict | None = None,
        *,
        risk_override: str | None = None,
    ) -> tuple[bool, str]:
        """检查工具是否可以执行。

        Returns:
            (allowed, reason) — allowed=True 表示可以执行；
            allowed=False 时 reason 是拒绝原因。
        """
        risk = risk_override or self._classify(tool_name, params)

        # PLAN 模式：只允许 READ
        if self.mode == PermissionMode.PLAN:
            if risk != "READ":
                return False, f"PLAN 模式禁止 {risk} 操作: {tool_name}"
            return True, ""

        # BYPASS 模式：全部允许
        if self.mode == PermissionMode.BYPASS:
            return True, ""

        # 会话级别记忆
        if (
            tool_name in self._session_allow
            or self._approval_key(tool_name, params or {}) in self._session_allow_exact
        ):
            return True, ""

        # ACCEPT_EDITS 模式：仅 CRITICAL 需要确认
        if self.mode == PermissionMode.ACCEPT_EDITS:
            if risk == "CRITICAL":
                return self._ask(tool_name, params or {}, risk)
            return True, ""

        # DEFAULT 模式：CRITICAL + WRITE 需要确认
        if self.mode == PermissionMode.DEFAULT:
            if risk in ("CRITICAL", "WRITE"):
                return self._ask(tool_name, params or {}, risk)
            return True, ""

        return True, ""

    def _ask(self, tool_name: str, params: dict, risk: str) -> tuple[bool, str]:
        """向用户确认。返回 (allowed, reason)。"""
        if self._confirm_callback is not None:
            return self._confirm_callback(tool_name, params, risk)

        # 没有交互确认能力时必须 fail closed。自动化调用方如确实需要跳过
        # 确认，应显式选择 BYPASS，而不是让 DEFAULT 静默失效。
        return False, f"{risk} 操作需要确认，但当前没有可用的确认回调"

    def allow_always(self, tool_name: str) -> None:
        """会话级别：总是允许此工具。"""
        self._session_allow.add(tool_name)

    @staticmethod
    def _approval_key(tool_name: str, params: dict) -> str:
        """Return a bounded, non-reversible signature for an exact action."""
        payload = json.dumps(
            params,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=repr,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"{tool_name}:{digest}"

    def allow_exact(self, tool_name: str, params: dict) -> None:
        """本会话放行参数完全相同的一项 CRITICAL 操作。"""
        self._session_allow_exact.add(self._approval_key(tool_name, params))

    def reset_session(self) -> None:
        """清除会话级别记忆。"""
        self._session_allow.clear()
        self._session_allow_exact.clear()

    @staticmethod
    def format_confirm_message(tool_name: str, params: dict, risk: str) -> str:
        """格式化确认提示消息。"""
        lines = [f"⚠️  {risk} 操作: [bold yellow]{tool_name}[/bold yellow]"]

        if tool_name == "command":
            # ToolExecutor normalizes command/cmd to ``action`` before the
            # permission gate, so action must be the primary display field.
            cmd = params.get("action", params.get("command", params.get("cmd", "?")))
            lines.append(f"   命令: [bold white]{escape(str(cmd))}[/bold white]")
        elif tool_name == "write_file":
            path = params.get("file_path", params.get("path", "?"))
            lines.append(f"   写入: [bold white]{escape(str(path))}[/bold white]")
        elif tool_name == "edit_file":
            path = params.get("file_path", params.get("path", "?"))
            lines.append(f"   编辑: [bold white]{escape(str(path))}[/bold white]")
        elif tool_name == "git":
            action = params.get("git_command", params.get("action", "?"))
            lines.append(f"   操作: [bold white]{escape(str(action))}[/bold white]")
        elif tool_name == "create_directory":
            path = params.get("file_path", params.get("path", "?"))
            lines.append(f"   创建: [bold white]{escape(str(path))}[/bold white]")
        elif tool_name == "mcp_call":
            raw_target = str(params.get("tool_name") or "未指定工具")
            raw_server = str(params.get("mcp_server") or "")
            if not raw_server and ":" in raw_target:
                raw_server, raw_target = raw_target.split(":", 1)
            server = escape(raw_server or "自动路由")
            target = escape(raw_target)
            lines.append(f"   MCP: [bold white]{server} / {target}[/bold white]")

        lines.append("")
        always_label = "本会话允许相同操作" if risk == "CRITICAL" else "本次会话总是允许"
        # This string is rendered as Rich markup by the REPL.  Bare ``[y]``
        # looks like a markup tag and is silently removed, so escape literal
        # key brackets while keeping the keys visually prominent.
        y_key = escape("[y]")
        n_key = escape("[n]")
        a_key = escape("[a]")
        q_key = escape("[q]")
        lines.append(
            f"   [bold cyan]{y_key}[/bold cyan] 确认  "
            f"[bold cyan]{n_key}[/bold cyan] 拒绝  "
            f"[bold cyan]{a_key}[/bold cyan] {always_label}  "
            f"[bold cyan]{q_key}[/bold cyan] 取消任务"
        )
        return "\n".join(lines)
