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
import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from rich.markup import escape

logger = logging.getLogger(__name__)


class PermissionMode(Enum):
    """权限模式。"""
    DEFAULT = "default"           # 危险操作确认
    ACCEPT_EDITS = "accept_edits" # 自动批准编辑
    BYPASS = "bypass"            # 跳过确认
    PLAN = "plan"                # 只读模式


class PermissionState(Enum):
    """State of the most recent permission decision.

    Keeping this state in the gate makes the confirmation lifecycle observable
    without changing the historical ``check() -> (bool, reason)`` contract.
    """

    IDLE = "idle"
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PermissionDecision(Enum):
    """Normalized decisions accepted by future confirmation frontends."""

    ALLOW_ONCE = "allow_once"
    DENY = "deny"
    ALLOW_SESSION = "allow_session"
    CANCEL = "cancel"


@dataclass(frozen=True)
class PermissionRequest:
    """Immutable description of one operation awaiting user approval."""

    tool_name: str
    params: dict[str, Any]
    risk: str


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

_SENSITIVE_DISPLAY_KEYS: frozenset[str] = frozenset({
    "content", "token", "api_key", "apikey", "password", "secret",
    "credential", "credentials", "authorization", "python_function",
    "command_template",
})


def _safe_display_value(key: str, value: Any) -> Any:
    """Recursively summarize confirmation parameters without leaking secrets."""
    if key.casefold() in _SENSITIVE_DISPLAY_KEYS:
        size = len(value) if isinstance(value, str) else "?"
        return f"<masked len={size}>"
    if isinstance(value, dict):
        return {
            str(child_key): _safe_display_value(str(child_key), child_value)
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_display_value(key, child) for child in value[:50]]
    text = str(value)
    return text if len(text) <= 500 else text[:497] + "..."


def _safe_display_params(params: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): _safe_display_value(str(key), value)
        for key, value in params.items()
    }


class PermissionGate:
    """工具执行权限门控。

    在工具执行前检查是否需要用户确认，
    支持会话级别的"总是允许"记忆。
    """

    def __init__(self, mode: PermissionMode = PermissionMode.DEFAULT) -> None:
        self.mode = mode
        # 会话级别记忆：用户选择"总是允许"的工具名集合
        self._session_allow: set[str] = set()
        # ``allow_exact`` remains available for integrations that want a
        # narrow approval. Interactive [a] / ALLOW_SESSION is intentionally
        # broader: it approves later calls of this tool in the same session.
        self._session_allow_exact: set[str] = set()
        # 外部确认回调：签名 (tool_name, params, risk_level) -> bool
        self._confirm_callback: Callable[[str, dict, str], Any] | None = None
        # Engines may execute independent tool calls in worker threads.  Keep
        # the gate's state transition and confirmation callback atomic so two
        # workers cannot open competing stdin prompts.
        self._ask_lock = threading.RLock()
        self._state = PermissionState.IDLE
        self._last_request: PermissionRequest | None = None

        # ★ Phase 2: 执行策略集成
        self._current_execution_level: int | None = None
        self._current_request_text: str | None = None

    def set_confirm_callback(self, cb: Callable[[str, dict, str], Any]) -> None:
        """设置确认回调（由 REPL 层注入）。"""
        self._confirm_callback = cb

    def set_mode(self, mode: PermissionMode) -> None:
        """切换权限模式。"""
        self.mode = mode
        # A mode change is an authorization boundary. Do not carry an
        # earlier session-level [a] approval into PLAN/BYPASS/DEFAULT changes.
        self.reset_session()

    def set_execution_context(
        self,
        execution_level: int,
        request_text: str,
    ) -> None:
        """
        设置当前请求的执行上下文（Phase 2 新增）

        由 REPL 在处理用户输入时调用，将 ExecutionPolicy 传递给权限门。

        Args:
            execution_level: ExecutionLevel 的整数值
                - 0: ANSWER_ONLY
                - 1: READ_ONLY
                - 2: WRITE
                - 3: EXECUTE
            request_text: 用户原始请求（用于确认框显示）
        """
        self._current_execution_level = execution_level
        self._current_request_text = request_text

    @property
    def state(self) -> PermissionState:
        """Current state of the last permission check."""
        return self._state

    @property
    def last_request(self) -> PermissionRequest | None:
        """Most recent request, useful for UI/tests and audit logs."""
        return self._last_request

    @property
    def session_allowed_tools(self) -> tuple[str, ...]:
        """Read-only view used by the permissions status command."""
        return tuple(sorted(self._session_allow))

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

        # risk_override 是 ToolExecutor 掌握的最高风险信息（尤其覆盖运行时
        # 注册的动态工具——它们的名称不在 _classify 的静态工具表中）。非法
        # 值绝不能静默 fallthrough 到「允许」：权限层遇到无法识别的风险
        # 等级必须 fail-closed。
        _VALID_RISKS = {"READ", "WRITE", "CRITICAL"}
        if risk not in _VALID_RISKS:
            logger.warning(
                "权限闸门收到未知风险等级 %r（tool=%s），按 CRITICAL 处理",
                risk, tool_name,
            )
            risk = "CRITICAL"

        # PLAN 模式：只允许 READ
        if self.mode == PermissionMode.PLAN:
            if risk != "READ":
                self._state = PermissionState.DENIED
                return False, f"PLAN 模式禁止 {risk} 操作: {tool_name}"
            self._state = PermissionState.APPROVED
            return True, ""

        # BYPASS 模式：全部允许
        if self.mode == PermissionMode.BYPASS:
            self._state = PermissionState.APPROVED
            return True, ""

        # ★ Phase 2: 检查执行策略是否已覆盖该风险级别
        # （在 PLAN/BYPASS 之后，在会话记忆之前）
        if self._current_execution_level is not None:
            # 风险级别到执行级别的映射
            risk_to_level = {
                "READ": 1,      # ExecutionLevel.READ_ONLY
                "WRITE": 2,     # ExecutionLevel.WRITE
                "CRITICAL": 3,  # ExecutionLevel.EXECUTE
            }
            required_level = risk_to_level.get(risk, 3)

            if self._current_execution_level >= required_level:
                # 用户已在原始请求中明确授权此级别的操作
                self._state = PermissionState.APPROVED
                return True, "用户已在原始请求中授权此操作"

        # 会话级别记忆
        if (
            tool_name in self._session_allow
            or self._approval_key(tool_name, params or {}) in self._session_allow_exact
        ):
            self._state = PermissionState.APPROVED
            return True, ""

        # ACCEPT_EDITS 模式：仅 CRITICAL 需要确认
        if self.mode == PermissionMode.ACCEPT_EDITS:
            if risk == "CRITICAL":
                return self._ask(tool_name, params or {}, risk)
            self._state = PermissionState.APPROVED
            return True, ""

        # DEFAULT 模式：CRITICAL + WRITE 需要确认
        if self.mode == PermissionMode.DEFAULT:
            if risk in ("CRITICAL", "WRITE"):
                return self._ask(tool_name, params or {}, risk)
            self._state = PermissionState.APPROVED
            return True, ""

        self._state = PermissionState.APPROVED
        return True, ""

    def _ask(self, tool_name: str, params: dict, risk: str) -> tuple[bool, str]:
        with self._ask_lock:
            # A parallel worker may have passed the optimistic session-cache
            # check just before another worker approved this exact operation.
            # Re-check under the serialization lock so an [a] decision really
            # suppresses duplicate prompts already queued by that wave.
            if (
                tool_name in self._session_allow
                or self._approval_key(tool_name, params)
                in self._session_allow_exact
            ):
                self._state = PermissionState.APPROVED
                return True, ""
            return self._ask_unlocked(tool_name, params, risk)

    def _ask_unlocked(self, tool_name: str, params: dict, risk: str) -> tuple[bool, str]:
        """向用户确认。返回 (allowed, reason)。"""
        self._last_request = PermissionRequest(tool_name, dict(params), risk)
        self._state = PermissionState.PENDING
        if self._confirm_callback is not None:
            try:
                response = self._confirm_callback(tool_name, params, risk)
            except Exception as exc:  # noqa: BLE001 - fail closed at the gate
                self._state = PermissionState.FAILED
                return False, f"确认回调失败: {exc}"
            allowed, reason, decision = self._normalize_response(response)
            if allowed:
                if decision is PermissionDecision.ALLOW_SESSION:
                    self.allow_always(tool_name)
                self._state = PermissionState.APPROVED
            elif decision is PermissionDecision.CANCEL or "取消" in reason:
                self._state = PermissionState.CANCELLED
            else:
                self._state = PermissionState.DENIED
            return allowed, reason

        # 没有交互确认能力时必须 fail closed。自动化调用方如确实需要跳过
        # 确认，应显式选择 BYPASS，而不是让 DEFAULT 静默失效。
        self._state = PermissionState.FAILED
        return False, f"{risk} 操作需要确认，但当前没有可用的确认回调"

    @staticmethod
    def _normalize_response(
        response: Any,
    ) -> tuple[bool, str, PermissionDecision | None]:
        """Accept legacy tuples/bools and the new decision enum."""
        if isinstance(response, PermissionDecision):
            if response is PermissionDecision.ALLOW_ONCE:
                return True, "", response
            if response is PermissionDecision.ALLOW_SESSION:
                return True, "", response
            if response is PermissionDecision.CANCEL:
                return False, "用户取消任务", response
            return False, "用户拒绝", response
        if isinstance(response, tuple) and len(response) == 2:
            allowed, reason = response
            if isinstance(allowed, bool):
                return allowed, str(reason or ""), None
        if isinstance(response, bool):
            return response, "" if response else "用户拒绝", None
        return False, "确认回调返回了无效结果", None

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
        self._state = PermissionState.IDLE
        self._last_request = None

    def format_confirm_message(self, tool_name: str, params: dict, risk: str) -> str:
        """格式化确认提示消息（Phase 2：显示用户原始请求）。"""
        lines = [f"⚠️  {risk} 操作: [bold yellow]{tool_name}[/bold yellow]"]

        # ★ Phase 2: 显示用户原始请求
        if self._current_request_text:
            request_preview = self._current_request_text
            if len(request_preview) > 60:
                request_preview = request_preview[:57] + "..."
            lines.append(f"   原始请求: [dim]{escape(request_preview)}[/dim]")

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

        # Keep the approval surface useful for tools that do not have a
        # bespoke one-line renderer (batch writes, refactors, dynamic tools,
        # and future MCP wrappers).  Sensitive values are summarized rather
        # than printed into terminal history.
        if tool_name not in {
            "command", "write_file", "edit_file", "git", "create_directory", "mcp_call",
        } and params:
            safe = _safe_display_params(params)
            rendered = json.dumps(safe, ensure_ascii=False, sort_keys=True)
            lines.append(
                f"   参数: [bold white]{escape(rendered)}[/bold white]"
            )

        lines.append("")
        always_label = "本会话总是允许此工具" if risk == "CRITICAL" else "本次会话总是允许"
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
