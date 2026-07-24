"""ToolExecutor — 工具执行门面 + 7 阶段流水线（F1 / Q3）。

包裹 ``ToolNode``（保留其接口不动，向后兼容），串起：
  1. 标准化（normalize_params）
  2. 参数幻觉校验（_validate_tool_params）
  3. 工具分类（INFO/WRITE/SENSITIVE）+ 权限闸门
  4. 断路器（CircuitBreaker.allow）
  5. 执行（委托 ToolNode）
  6. 重试（is_terminal_error 区分瞬时/终端）
  7. 结果封装（ToolExecuteResult + tracker 记录）

四引擎把 ``ToolNode(...).execute()`` 换成 ``ToolExecutor().execute(tool, params, ctx, tracker)``。
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from xenon.engine.circuit_breaker import BreakerRegistry
from xenon.engine.context import AgentContext
from xenon.engine.tool_tracker import ToolExecutionTracker
from xenon.nodes.tool_node import ToolNode, _DYNAMIC_TOOLS
from xenon.nodes.tool_result import ToolResult, enrich_tool_result
from xenon.engine.callbacks import mask_sensitive_params

logger = logging.getLogger(__name__)


# ── 工具分类 ───────────────────────────────────────────────
_SENSITIVE_TOOLS = {"command"}  # 任意 shell 执行——最高风险
_WRITE_TOOLS = {
    "write_file", "edit_file", "create_directory",
    "batch_write", "batch_edit", "edit_with_llm", "append_file",
    "git", "refactor", "register_tool", "clone_repo",
}
# 其余按 INFO 处理（read_file/list_files/search_files/web_fetch/github_fetch...）

# 参数幻觉校验豁免白名单：这些参数合法持有长文本/代码，不参与结构性检查
_TOOL_CONTENT_PARAMS = frozenset({
    "content", "old_text", "new_text", "code", "text",
    "diff", "patch", "replacement", "snippet",
})


def classify_tool(
    tool_name: str,
    params: dict[str, Any] | None = None,
) -> str:
    """返回 INFO | WRITE | SENSITIVE，必要时细分 MCP 远端能力。"""
    if tool_name == "mcp_call" and params:
        remote_name = str(params.get("tool_name", ""))
        if _EXECUTING_MCP_NAME.search(remote_name):
            return "SENSITIVE"
        if _MUTATING_MCP_NAME.search(remote_name):
            return "WRITE"
        if _READ_ONLY_MCP_NAME.search(remote_name):
            return "INFO"
        return "SENSITIVE"
    if (
        tool_name in _SENSITIVE_TOOLS
        or tool_name == "mcp_call"
        or tool_name in _DYNAMIC_TOOLS
    ):
        return "SENSITIVE"
    if tool_name in _WRITE_TOOLS:
        return "WRITE"
    return "INFO"


_MUTATING_MCP_NAME = re.compile(
    r"(?:^|[:/_.-])(?:create|write|save|edit|update|delete|remove|insert|"
    r"append|set|add|send|post|put|publish|deploy|commit|merge|execute|run)"
    r"(?:$|[:/_.-])",
    re.IGNORECASE,
)
_EXECUTING_MCP_NAME = re.compile(
    r"(?:^|[:/_.-])(?:execute|run|command|shell|terminal|deploy)"
    r"(?:$|[:/_.-])",
    re.IGNORECASE,
)
_READ_ONLY_MCP_NAME = re.compile(
    r"(?:^|[:/_.-])(?:get|list|search|read|fetch|query|find|lookup|inspect|"
    r"view|show|browse|navigate|open|weather|time|date|status|describe)"
    r"(?:$|[:/_.-])",
    re.IGNORECASE,
)


def required_execution_level(tool_name: str, params: dict[str, Any]) -> int:
    """Return the minimum per-turn execution level required by a tool.

    Values intentionally match ``ExecutionLevel`` without importing the REPL
    package into the low-level executor: 1=read, 2=write, 3=execute.
    """

    if tool_name == "mcp_call":
        remote_name = str(params.get("tool_name", ""))
        if not remote_name:
            # Schema construction has no call parameters yet. Keep the MCP
            # transport visible; the concrete remote name is checked again at
            # execution time before any request is sent.
            return 1
        if _EXECUTING_MCP_NAME.search(remote_name):
            return 3
        if _MUTATING_MCP_NAME.search(remote_name):
            return 2
        if _READ_ONLY_MCP_NAME.search(remote_name):
            return 1
        # Unknown remote tools are not assumed to be read-only merely because
        # they travel through a generic MCP transport.
        return 3
    if tool_name in _SENSITIVE_TOOLS or tool_name in _DYNAMIC_TOOLS:
        return 3
    if tool_name in _WRITE_TOOLS or tool_name == "create_skill":
        return 2
    if tool_name == "spawn_agent":
        # A delegated agent could otherwise bypass the parent turn's boundary.
        return 3
    return 1


def execution_policy_denial(
    tool_name: str,
    params: dict[str, Any],
    context: AgentContext,
) -> str | None:
    """Return a hard-denial reason when a tool exceeds this turn's policy."""

    authorized = context.get("_execution_level")
    if authorized is None:
        # Backward compatibility for direct engine/library users that have not
        # opted into REPL-level policy classification.
        return None
    required = required_execution_level(tool_name, params)
    if int(authorized) >= required:
        return None
    labels = {0: "仅回答", 1: "只读", 2: "可写入", 3: "可执行"}
    return (
        f"本轮执行策略为“{labels.get(int(authorized), str(authorized))}”，"
        f"不允许工具 {tool_name} 所需的“{labels[required]}”权限。"
        "如需扩大范围，请由用户在新指令中明确提出。"
    )


# ── 参数幻觉校验（7 类正则，组合判定） ─────────────────────
_RE_FUNC_SIG = re.compile(r"\)\s*->\s*:|def\s+\w+\s*\([^)]*\)\s*:")
_RE_WIN_ILLEGAL = re.compile(r"[<>|*?\"]")
_RE_TRAILING_ILLEGAL = re.compile(r"[])}\"']+$")


def _chinese_ratio(s: str) -> float:
    if not s:
        return 0.0
    cjk = sum(1 for c in s if "一" <= c <= "鿿")
    return cjk / len(s)


def _code_structure_ratio(s: str) -> float:
    """代码结构字符（;{}()=<>）占比。"""
    if not s:
        return 0.0
    code_chars = sum(1 for c in s if c in ";{}()=<>")
    return code_chars / len(s)


def _balanced(s: str) -> bool:
    """括号/方括号是否配平。"""
    pairs = {"(": ")", "[": "]"}
    stack: list[str] = []
    for c in s:
        if c in pairs:
            stack.append(c)
        elif c in pairs.values():
            if not stack or pairs[stack.pop()] != c:
                return False
    return not stack


def _validate_param_value(name: str, value: Any) -> list[str]:
    """对单个非 content 参数做 7 类检查，返回命中的条件描述列表。

    v0.5.3: 放宽 shell here-doc、Python -c 和长命令的检查。
    """
    if not isinstance(value, str) or not value:
        return []

    # v0.5.3: 检测 shell here-doc 和 Python -c 模式，这些是合法的命令形式
    _looks_like_heredoc = bool(re.search(r"<<\s*['\"]?\w+['\"]?", value))
    _looks_like_python_c = bool(re.search(r"python\d*\s+-c\s", value))
    _is_shell_payload = _looks_like_heredoc or _looks_like_python_c

    hits: list[str] = []
    # v0.5.3: here-doc 和 python -c 中的代码不应被标记为函数签名
    if _RE_FUNC_SIG.search(value) and not _is_shell_payload:
        hits.append("疑似函数签名")
    if not _balanced(value):
        hits.append("括号不配平")
    if name in ("file_path", "path", "dir", "directory") and _RE_WIN_ILLEGAL.search(value):
        hits.append("Windows 非法字符")
    if _RE_TRAILING_ILLEGAL.search(value):
        hits.append("末尾非法字符")
    # v0.5.3: command/action 放宽到 2000 字符（shell 命令和 Python -c 可以很长）
    cmd_max_len = 2000 if _is_shell_payload else 200
    if name in ("file_path", "path", "command", "action") and len(value) > cmd_max_len:
        hits.append(f"超长(>{cmd_max_len})")
    if name in ("file_path", "path", "command") and _chinese_ratio(value) > 0.5:
        hits.append("中文占比过高")
    if _code_structure_ratio(value) > 0.3 and len(value) > 50:
        hits.append("纯代码结构")
    return hits


def validate_tool_params(params: dict[str, Any]) -> tuple[bool, str]:
    """参数幻觉校验：组合判定（≥2 条件命中才拦），content 白名单豁免。

    Returns:
        (ok, reason) — ok=False 时 reason 描述命中条件。
    """
    for name, value in params.items():
        if name in _TOOL_CONTENT_PARAMS:
            continue
        hits = _validate_param_value(name, value)
        if len(hits) >= 2:
            return False, f"参数 '{name}' 疑似 LLM 幻觉（命中: {'; '.join(hits)}）"
    return True, ""


# ── 错误分类 ───────────────────────────────────────────────
_TERMINAL_PATTERNS = re.compile(
    r"文件不存在|不存在|not found|no such|找不到|permission denied|"
    r"权限拒绝|access denied|参数非法|非法参数|invalid param|illegal|"
    r"is a directory|not a directory|already exists|已存在|"
    r"不支持|unsupported|unknown action",
    re.IGNORECASE,
)
_TRANSIENT_PATTERNS = re.compile(
    r"timeout|timed out|限流|429|rate limit|connection|econn|"
    r"temporar|暂时|重试|retry|broken pipe|reset",
    re.IGNORECASE,
)


# v0.5.3: 参数校验拦截时提示替代工具，帮助 LLM 恢复
_TOOL_ALTERNATIVES: dict[str, list[str]] = {
    "command": ["search_files（搜索文件内容）", "read_file（读取文件）", "list_files（列出文件）"],
    "write_file": ["batch_write（批量写入）"],
    "edit_file": ["batch_edit（批量编辑）"],
}


def _tool_alternative_hint(tool_name: str, params: dict[str, object]) -> str:
    """参数校验拦截时，返回替代工具提示。"""
    alternatives = _TOOL_ALTERNATIVES.get(tool_name, [])
    if not alternatives:
        return ""
    # 根据参数内容推断更具体的建议
    param_str = " ".join(str(v) for v in params.values() if isinstance(v, str))[:200]
    suggestions: list[str] = []
    for alt in alternatives:
        alt_name = alt.split("（")[0]
        if alt_name in param_str:
            continue  # 已经在用类似的，不重复建议
        suggestions.append(alt)
    if not suggestions:
        suggestions = alternatives
    return f"\n💡 建议使用: {' | '.join(suggestions[:3])}"


def is_terminal_error(error: str) -> bool:
    """终端错误（文件不存在/权限/参数非法）不重试；瞬时错误（超时/限流/网络）重试。"""
    if not error:
        return False
    if _TRANSIENT_PATTERNS.search(error):
        return False
    return bool(_TERMINAL_PATTERNS.search(error))


def is_timeout_error(error: str) -> bool:
    """Return whether an error represents an execution timeout."""
    return bool(re.search(r"timeout|timed out|超时", error or "", re.IGNORECASE))


class ToolExecutionState(str, Enum):
    """Stable lifecycle states shared by engines, sessions, and the REPL."""

    PENDING = "pending"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


_UNFINISHED_TOOL_STATES = frozenset({
    ToolExecutionState.PENDING.value,
    ToolExecutionState.RUNNING.value,
    ToolExecutionState.RETRYING.value,
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resume_action(
    state: ToolExecutionState,
    tool_class: str,
    *,
    retryable: bool,
) -> str:
    if state in {ToolExecutionState.SUCCEEDED, ToolExecutionState.CANCELLED}:
        return "none"
    if tool_class != "INFO":
        if state in {
            ToolExecutionState.PENDING,
            ToolExecutionState.RUNNING,
            ToolExecutionState.RETRYING,
            ToolExecutionState.TIMED_OUT,
            ToolExecutionState.INTERRUPTED,
        }:
            return "manual_verification"
        return "change_parameters"
    if retryable or state in {
        ToolExecutionState.TIMED_OUT,
        ToolExecutionState.INTERRUPTED,
    }:
        return "retry"
    return "change_parameters"


def _record_lifecycle_checkpoint(
    context: AgentContext,
    events: list[dict[str, Any]],
    *,
    execution_id: str,
    tool_name: str,
    tool_class: str,
    state: ToolExecutionState,
    params: dict[str, Any],
    attempt: int,
    max_attempts: int,
    started_at: str,
    started_monotonic: float,
    retryable: bool = False,
    error_kind: str | None = None,
) -> dict[str, Any]:
    """Record a bounded, serializable checkpoint without argument values.

    Tool arguments can contain source code, credentials, shell commands, or
    user data.  Recovery only needs identity and policy metadata, so values are
    intentionally never persisted here.
    """
    elapsed = max(0.0, time.monotonic() - started_monotonic)
    recoverable = (
        tool_class == "INFO"
        and state
        in {
            ToolExecutionState.RETRYING,
            ToolExecutionState.FAILED,
            ToolExecutionState.TIMED_OUT,
            ToolExecutionState.INTERRUPTED,
        }
    )
    status_unknown = (
        tool_class != "INFO"
        and attempt > 0
        and (
            state in {
                ToolExecutionState.TIMED_OUT,
                ToolExecutionState.INTERRUPTED,
            }
            or error_kind == "transient"
        )
    )
    checkpoint: dict[str, Any] = {
        "schema_version": "1.0",
        "execution_id": execution_id,
        "tool_name": tool_name,
        "tool_class": tool_class,
        "state": state.value,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "started_at": started_at,
        "updated_at": _utc_now(),
        "elapsed_seconds": round(elapsed, 6),
        "retryable": bool(retryable and tool_class == "INFO"),
        "recoverable": recoverable,
        "requires_confirmation": tool_class != "INFO",
        "parameter_names": sorted(str(key) for key in params),
        "resume_action": _resume_action(
            state,
            tool_class,
            retryable=retryable,
        ),
    }
    if status_unknown:
        checkpoint["status_unknown"] = True
        checkpoint["resume_action"] = "manual_verification"
    if error_kind:
        checkpoint["error_kind"] = error_kind
    events.append(checkpoint.copy())
    if hasattr(context, "record_tool_checkpoint"):
        context.record_tool_checkpoint(checkpoint)
    else:  # pragma: no cover - compatibility for third-party context shims
        context.set("_tool_execution_checkpoint", checkpoint)
    return checkpoint


def recover_tool_execution_checkpoint(context: AgentContext) -> str:
    """Normalize unfinished durable checkpoints and return a user notice.

    Recovery never replays a tool.  Read-only work may be explicitly retried;
    stateful work is reported as status-unknown and requires verification. All
    concurrently active executions are recovered, not only the newest event.
    """
    active = context.get("_tool_execution_active", {})
    unfinished: list[tuple[str, dict[str, Any]]] = []
    if isinstance(active, dict):
        unfinished.extend(
            (str(execution_key), item)
            for execution_key, item in active.items()
            if isinstance(item, dict)
            and str(item.get("state", "")) in _UNFINISHED_TOOL_STATES
        )

    # Sessions written before the active-execution ledger only contain the
    # newest checkpoint. Keep that format recoverable during upgrades.
    current = context.get("_tool_execution_checkpoint")
    if not unfinished and isinstance(current, dict):
        if str(current.get("state", "")) in _UNFINISHED_TOOL_STATES:
            unfinished.append((str(current.get("execution_id", "")), current))

    restored_items: list[dict[str, Any]] = []
    for execution_key, item in unfinished:
        restored = dict(item)
        # The active-ledger map key is authoritative for migrated entries. It
        # also lets record_tool_checkpoint remove malformed entries that lack
        # an execution_id instead of leaving them active forever.
        if execution_key:
            restored["execution_id"] = execution_key
        restored["state"] = ToolExecutionState.INTERRUPTED.value
        restored["updated_at"] = _utc_now()
        restored["error_kind"] = "process_interrupted"
        is_info = restored.get("tool_class") == "INFO"
        restored["retryable"] = is_info
        restored["recoverable"] = is_info
        restored["resume_action"] = "retry" if is_info else "manual_verification"
        if not is_info:
            restored["status_unknown"] = True
        if hasattr(context, "record_tool_checkpoint"):
            context.record_tool_checkpoint(restored)
        else:  # pragma: no cover
            context.set("_tool_execution_checkpoint", restored)
        restored_items.append(restored)

    if restored_items:
        details = []
        for restored in restored_items:
            tool_name = str(restored.get("tool_name", "unknown"))
            if restored.get("tool_class") == "INFO":
                details.append(f"{tool_name}（只读，可重新发起）")
            else:
                details.append(f"{tool_name}（可能已部分生效，须人工核验）")
        return (
            f"⚠️ 检测到 {len(restored_items)} 个上次未完成的工具执行，"
            "均已标记为 interrupted，且未自动重放：\n- "
            + "\n- ".join(details)
        )

    if not isinstance(current, dict):
        return ""
    state = str(current.get("state", ""))

    if state not in {
        ToolExecutionState.INTERRUPTED.value,
        ToolExecutionState.TIMED_OUT.value,
    }:
        return ""

    tool_name = str(current.get("tool_name", "unknown"))
    if current.get("tool_class") == "INFO":
        return (
            f"⚠️ 上次工具 {tool_name} 未完成（{state}），未自动重放；"
            "这是只读操作，可由用户重新发起。"
        )
    return (
        f"⚠️ 上次工具 {tool_name} 未完成（{state}），未自动重放；"
        "该操作可能已部分生效，请先人工核验后再决定是否重试。"
    )


# ── 结果封装 ───────────────────────────────────────────────
@dataclass
class ToolExecuteResult:
    """工具执行结果——observation/notification/next_hint 三视图。"""

    tool_name: str
    success: bool
    observation: str
    error: str | None = None
    tool_class: str = "INFO"
    attempts: int = 1
    raw: dict[str, Any] | None = field(default=None, repr=False)
    structured: ToolResult | None = field(default=None, repr=False)
    cancelled: bool = False
    state: ToolExecutionState | None = None
    elapsed_seconds: float = 0.0
    timed_out: bool = False
    retryable: bool = False
    recoverable: bool = False
    execution_id: str = ""
    lifecycle: tuple[dict[str, Any], ...] = field(default_factory=tuple, repr=False)

    def __post_init__(self) -> None:
        """Always expose a structured result, including early failures."""
        if not self.cancelled:
            self.cancelled = "取消任务" in (self.error or "")
        if not self.timed_out:
            self.timed_out = is_timeout_error(self.error or "")
        if self.state is None:
            if self.cancelled:
                self.state = ToolExecutionState.CANCELLED
            elif self.success:
                self.state = ToolExecutionState.SUCCEEDED
            elif self.timed_out:
                self.state = ToolExecutionState.TIMED_OUT
            else:
                self.state = ToolExecutionState.FAILED
        if self.structured is None:
            raw = self.raw
            if raw is None:
                raw = {
                    "action_type": self.tool_name,
                    "success": self.success,
                    "content": self.observation,
                    "error": self.error,
                }
            self.structured = ToolResult.from_raw(self.tool_name, raw)

    def format_observation(self) -> str:
        """供引擎回填给 LLM 的观察文本。"""
        return self.observation

    def next_hint(self) -> str:
        """按失败原因给情境化下一步提示。"""
        if self.success:
            return ""
        if self.cancelled:
            return f"工具 {self.tool_name} 已取消，不应自动重试。"
        if self.timed_out:
            if self.tool_class == "INFO":
                return f"只读工具 {self.tool_name} 超时，可稍后显式重试。"
            return (
                f"工具 {self.tool_name} 超时且状态未知；请先核验副作用，"
                "不要自动重试。"
            )
        err = (self.error or "").lower()
        if "不存在" in err or "not found" in err:
            return f"工具 {self.tool_name} 报告目标不存在，请先用 list_files 确认路径。"
        if "权限" in err or "permission" in err:
            return f"工具 {self.tool_name} 权限不足，请检查文件/目录权限或换路径。"
        if "已存在" in err or "already exists" in err:
            return f"工具 {self.tool_name} 目标已存在，若需覆盖请显式确认。"
        if "断路器" in (self.error or "") or "circuit" in err:
            return f"工具 {self.tool_name} 已熔断，请稍后重试或换用其它工具。"
        return f"工具 {self.tool_name} 执行失败，请检查参数或换一种方式。"


# ── 观察摘要提取（与原引擎逻辑一致） ───────────────────────
def _extract_summary(result: dict[str, Any], list_cap: int = 50, str_cap: int = 3000) -> str:
    """从工具结果中提取 LLM 可读的文本摘要。

    按优先级查找: content → stdout → output → files。
    兼容 MCP 嵌套响应 (result.content[].text)。
    """
    for key in ("content", "stdout", "output", "files"):
        val = result.get(key)
        if val:
            if isinstance(val, list):
                return "\n".join(str(v) for v in val[:list_cap])
            return str(val)[:str_cap]

    # v0.5.3: MCP 嵌套响应兜底 — result.result.content[].text
    inner = result.get("result", {})
    if isinstance(inner, dict):
        mcp_content = inner.get("content", [])
        if isinstance(mcp_content, list) and mcp_content:
            parts = []
            for item in mcp_content:
                if isinstance(item, dict):
                    parts.append(item.get("text", str(item)))
                else:
                    parts.append(str(item))
            if parts:
                return "\n".join(parts)[:str_cap]

    return "（执行成功，无文本输出）"


# ── 门面 ───────────────────────────────────────────────────
class ToolExecutor:
    """7 阶段工具执行门面。"""

    def __init__(
        self,
        *,
        retry_attempts: int = 2,
        breakers: BreakerRegistry | None = None,
        permission_gate: Any = None,  # v0.5.0: PermissionGate
    ) -> None:
        self.retry_attempts = max(1, retry_attempts)
        # 默认每引擎独立注册表（同引擎内跨 run 累积断路状态，且保证测试隔离）
        self.breakers = breakers or BreakerRegistry()
        self.permission_gate = permission_gate  # v0.5.0: 工具确认门控

    def execute(
        self,
        tool_name: str,
        params: dict[str, Any],
        context: AgentContext,
        tracker: ToolExecutionTracker | None = None,
        *,
        tools: dict[str, Any] | None = None,
    ) -> ToolExecuteResult:
        """执行工具，返回 ToolExecuteResult。"""
        tool_class = classify_tool(tool_name, params)
        execution_id = uuid.uuid4().hex
        started_at = _utc_now()
        started_monotonic = time.monotonic()
        lifecycle: list[dict[str, Any]] = []
        max_attempts = self.retry_attempts if tool_class == "INFO" else 1

        def transition(
            state: ToolExecutionState,
            *,
            attempt: int = 0,
            retryable: bool = False,
            error_kind: str | None = None,
        ) -> dict[str, Any]:
            return _record_lifecycle_checkpoint(
                context,
                lifecycle,
                execution_id=execution_id,
                tool_name=tool_name,
                tool_class=tool_class,
                state=state,
                params=params,
                attempt=attempt,
                max_attempts=max_attempts,
                started_at=started_at,
                started_monotonic=started_monotonic,
                retryable=retryable,
                error_kind=error_kind,
            )

        def finish(
            success: bool,
            observation: str,
            *,
            state: ToolExecutionState,
            error: str | None = None,
            attempts: int = 0,
            raw: dict[str, Any] | None = None,
            retryable: bool = False,
            error_kind: str | None = None,
        ) -> ToolExecuteResult:
            checkpoint = transition(
                state,
                attempt=attempts,
                retryable=retryable,
                error_kind=error_kind,
            )
            if checkpoint.get("status_unknown"):
                observation += (
                    "\n⚠️ 本次有副作用的操作状态未知，可能已部分生效；"
                    "必须先核验实际状态，不得自动重试。"
                )
            if tracker:
                tracker.record(
                    tool_name,
                    params,
                    success,
                    observation,
                    error=error,
                    state=state.value,
                    attempts=attempts,
                    elapsed_seconds=float(checkpoint["elapsed_seconds"]),
                )
            return ToolExecuteResult(
                tool_name=tool_name,
                success=success,
                observation=observation,
                error=error,
                tool_class=tool_class,
                attempts=attempts,
                raw=raw,
                cancelled=state is ToolExecutionState.CANCELLED,
                state=state,
                elapsed_seconds=float(checkpoint["elapsed_seconds"]),
                timed_out=state is ToolExecutionState.TIMED_OUT,
                retryable=bool(checkpoint["retryable"]),
                recoverable=bool(checkpoint["recoverable"]),
                execution_id=execution_id,
                lifecycle=tuple(lifecycle),
            )

        transition(ToolExecutionState.PENDING)

        # ── Stage 0: 工具存在性 ──
        if not self._tool_exists(tool_name, tools):
            msg = self._unknown_tool_msg(tool_name, tools)
            return finish(
                False,
                msg,
                state=ToolExecutionState.FAILED,
                error=msg,
                error_kind="unknown_tool",
            )

        # ── Stage 1: 标准化 ──
        try:
            try:
                params = ToolNode.normalize_params(params, action_type=tool_name)
            except TypeError as exc:
                # Compatibility for third-party/test ToolNode shims that still
                # expose the pre-0.7.1 one-argument normalizer.
                if "action_type" not in str(exc):
                    raise
                params = ToolNode.normalize_params(params)
        except Exception as e:  # noqa: BLE001
            msg = f"参数标准化失败: {e}"
            return finish(
                False,
                msg,
                state=ToolExecutionState.FAILED,
                error=msg,
                error_kind="invalid_parameters",
            )

        logger.debug(f"执行工具: {tool_name}, 参数: {mask_sensitive_params(params)}")

        # ── Stage 1.5: 本轮执行策略硬边界 ──
        policy_reason = execution_policy_denial(tool_name, params, context)
        if policy_reason:
            logger.info(f"执行策略拒绝: {tool_name} — {policy_reason}")
            return finish(
                False,
                f"⛔ {policy_reason}",
                state=ToolExecutionState.FAILED,
                error=policy_reason,
                error_kind="policy_denied",
            )

        # ── Stage 2: 参数幻觉校验 ──
        ok, reason = validate_tool_params(params)
        if not ok:
            logger.warning(f"参数幻觉拦截: {tool_name} — {reason}")
            # v0.5.3: 提示替代工具，帮助 LLM 恢复
            hint = _tool_alternative_hint(tool_name, params)
            err_msg = f"参数校验失败: {reason}"
            if hint:
                err_msg += hint
            return finish(
                False,
                err_msg,
                state=ToolExecutionState.FAILED,
                error=reason,
                error_kind="invalid_parameters",
            )

        # ── Stage 3: 权限闸门（v0.5.0: 接入 PermissionGate） ──
        if self.permission_gate is not None:
            # SENSITIVE 是执行器掌握的最高风险信息，尤其覆盖运行时注册的
            # 动态工具；后者的名称不在 PermissionGate 的静态工具表中。
            if tool_name == "mcp_call":
                risk_override = {
                    "INFO": "READ",
                    "WRITE": "WRITE",
                    "SENSITIVE": "CRITICAL",
                }[tool_class]
            else:
                risk_override = "CRITICAL" if tool_class == "SENSITIVE" else None
            allowed, reason = self.permission_gate.check(
                tool_name,
                params,
                risk_override=risk_override,
            )
            if not allowed:
                logger.info(f"权限拒绝: {tool_name} — {reason}")
                cancelled = "取消任务" in reason
                if cancelled:
                    # Engines share this context, so a user pressing q stops
                    # the current task instead of merely feeding a denial back
                    # to the model for another attempt.
                    context.set("_task_cancelled", True)
                return finish(
                    False,
                    f"⛔ 操作被拒绝: {reason}",
                    state=(
                        ToolExecutionState.CANCELLED
                        if cancelled
                        else ToolExecutionState.FAILED
                    ),
                    error=reason,
                    error_kind=("cancelled" if cancelled else "permission_denied"),
                )
        elif tool_class == "SENSITIVE":
            # 无 PermissionGate 时保持旧行为：仅记录
            logger.debug(f"SENSITIVE 工具调用: {tool_name}")

        # ── Stage 4: 断路器 ──
        breaker = self.breakers.get(tool_name)
        if not breaker.allow():
            msg = f"工具 {tool_name} 断路器开启（连败熔断），已拒绝调用"
            logger.warning(msg)
            return finish(
                False,
                msg,
                state=ToolExecutionState.FAILED,
                error=msg,
                retryable=tool_class == "INFO",
                error_kind="circuit_open",
            )

        # ── Stage 5+6: 执行 + 重试 ──
        last_error: str | None = None
        attempts = 0
        raw: dict[str, Any] | None = None
        # A stateful or executing tool may have partially completed before a
        # timeout.  Replaying it automatically can duplicate writes, commands,
        # commits or long clones.  Only read-only INFO tools are retryable, and
        # an individual tool result can explicitly opt out after doing its own
        # fallback (for example GitHub API -> public HTML).
        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            transition(ToolExecutionState.RUNNING, attempt=attempt)
            try:
                node = ToolNode(f"exec_{tool_name}", action_type=tool_name, **params)
                result = enrich_tool_result(tool_name, node.execute(context))
                raw = result
                if result.get("success", False):
                    breaker.record_success()
                    summary = _extract_summary(
                        result,
                        str_cap=(
                            12000
                            if tool_name == "docs_fetch" or result.get("prefilter_applied")
                            else 3000
                        ),
                    )
                    return finish(
                        True,
                        summary,
                        state=ToolExecutionState.SUCCEEDED,
                        attempts=attempts,
                        raw=raw,
                    )
                # 执行返回失败
                last_error = str(result.get("error") or result)
                breaker.record_failure()
                terminal = is_terminal_error(last_error)
                can_retry = (
                    result.get("retryable") is not False
                    and not terminal
                    and attempt < max_attempts
                )
                if can_retry:
                    transition(
                        ToolExecutionState.RETRYING,
                        attempt=attempt,
                        retryable=True,
                        error_kind=(
                            "timeout" if is_timeout_error(last_error) else "transient"
                        ),
                    )
                    logger.debug(
                        f"工具 {tool_name} 第 {attempt} 次失败（瞬时）: "
                        f"{last_error[:120]}"
                    )
                    continue
                final_state = (
                    ToolExecutionState.TIMED_OUT
                    if is_timeout_error(last_error)
                    else ToolExecutionState.FAILED
                )
                return finish(
                    False,
                    f"工具执行失败: {last_error}",
                    state=final_state,
                    error=last_error,
                    attempts=attempts,
                    raw=raw,
                    retryable=(
                        tool_class == "INFO"
                        and result.get("retryable") is not False
                        and not terminal
                    ),
                    error_kind=(
                        "timeout"
                        if final_state is ToolExecutionState.TIMED_OUT
                        else ("terminal" if terminal else "transient")
                    ),
                )
            except KeyboardInterrupt:
                context.set("_task_cancelled", True)
                return finish(
                    False,
                    "工具执行已由用户中断",
                    state=ToolExecutionState.CANCELLED,
                    error="用户取消任务",
                    attempts=attempts,
                    raw=raw,
                    error_kind="cancelled",
                )
            except Exception as e:  # noqa: BLE001 — 单次执行异常归为失败
                last_error = f"{type(e).__name__}: {e}"
                breaker.record_failure()
                logger.error(f"工具 {tool_name} 执行异常: {e}")
                terminal = is_terminal_error(last_error)
                if not terminal and attempt < max_attempts:
                    transition(
                        ToolExecutionState.RETRYING,
                        attempt=attempt,
                        retryable=True,
                        error_kind=(
                            "timeout" if is_timeout_error(last_error) else "transient"
                        ),
                    )
                    continue
                final_state = (
                    ToolExecutionState.TIMED_OUT
                    if is_timeout_error(last_error)
                    else ToolExecutionState.FAILED
                )
                return finish(
                    False,
                    f"工具执行失败: {last_error}",
                    state=final_state,
                    error=last_error,
                    attempts=attempts,
                    raw=raw,
                    retryable=tool_class == "INFO" and not terminal,
                    error_kind=(
                        "timeout"
                        if final_state is ToolExecutionState.TIMED_OUT
                        else ("terminal" if terminal else "transient")
                    ),
                )

        # Defensive fallback; each loop branch above returns explicitly.
        obs = f"工具执行失败: {last_error}"
        return finish(
            False,
            obs,
            state=ToolExecutionState.FAILED,
            error=str(last_error),
            attempts=attempts,
            raw=raw,
            error_kind="unknown",
        )

    # ── 辅助 ──
    def _tool_exists(self, tool_name: str, tools: dict[str, Any] | None) -> bool:
        # 未提供注册表（如 Plan-Execute）→ 交给 ToolNode 分发判定，不预检
        if tools is None:
            return True
        if tool_name in tools:
            return True
        return tool_name in _DYNAMIC_TOOLS

    def _unknown_tool_msg(self, tool_name: str, tools: dict[str, Any] | None) -> str:
        available = list((tools or {}).keys()) + list(_DYNAMIC_TOOLS.keys())
        return f"错误: 未知工具 '{tool_name}'，可用工具: {available}"
