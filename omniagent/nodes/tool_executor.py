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
from dataclasses import dataclass, field
from typing import Any

from omniagent.engine.circuit_breaker import GLOBAL_BREAKERS, BreakerRegistry
from omniagent.engine.context import AgentContext
from omniagent.engine.tool_tracker import ToolExecutionTracker
from omniagent.nodes.tool_node import ToolNode, _DYNAMIC_TOOLS
from omniagent.engine.callbacks import mask_sensitive_params

logger = logging.getLogger(__name__)


# ── 工具分类 ───────────────────────────────────────────────
_SENSITIVE_TOOLS = {"command"}  # 任意 shell 执行——最高风险
_WRITE_TOOLS = {
    "write_file", "edit_file", "create_directory",
    "batch_write", "batch_edit", "edit_with_llm", "append_file",
}
# 其余按 INFO 处理（read_file/list_files/search_files/web_fetch/github_fetch...）

# 参数幻觉校验豁免白名单：这些参数合法持有长文本/代码，不参与结构性检查
_TOOL_CONTENT_PARAMS = frozenset({
    "content", "old_text", "new_text", "code", "text",
    "diff", "patch", "replacement", "snippet",
})


def classify_tool(tool_name: str) -> str:
    """返回 INFO | WRITE | SENSITIVE。"""
    if tool_name in _SENSITIVE_TOOLS:
        return "SENSITIVE"
    if tool_name in _WRITE_TOOLS:
        return "WRITE"
    return "INFO"


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
    """对单个非 content 参数做 7 类检查，返回命中的条件描述列表。"""
    if not isinstance(value, str) or not value:
        return []
    hits: list[str] = []
    if _RE_FUNC_SIG.search(value):
        hits.append("疑似函数签名")
    if not _balanced(value):
        hits.append("括号不配平")
    if name in ("file_path", "path", "dir", "directory") and _RE_WIN_ILLEGAL.search(value):
        hits.append("Windows 非法字符")
    if _RE_TRAILING_ILLEGAL.search(value):
        hits.append("末尾非法字符")
    if name in ("file_path", "path", "command", "action") and len(value) > 200:
        hits.append("超长(>200)")
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


def is_terminal_error(error: str) -> bool:
    """终端错误（文件不存在/权限/参数非法）不重试；瞬时错误（超时/限流/网络）重试。"""
    if not error:
        return False
    if _TRANSIENT_PATTERNS.search(error):
        return False
    return bool(_TERMINAL_PATTERNS.search(error))


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

    def format_observation(self) -> str:
        """供引擎回填给 LLM 的观察文本。"""
        return self.observation

    def next_hint(self) -> str:
        """按失败原因给情境化下一步提示。"""
        if self.success:
            return ""
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
    for key in ("content", "stdout", "output", "files"):
        val = result.get(key)
        if val:
            if isinstance(val, list):
                return "\n".join(str(v) for v in val[:list_cap])
            return str(val)[:str_cap]
    return "（执行成功，无文本输出）"


# ── 门面 ───────────────────────────────────────────────────
class ToolExecutor:
    """7 阶段工具执行门面。"""

    def __init__(
        self,
        *,
        retry_attempts: int = 2,
        breakers: BreakerRegistry | None = None,
    ) -> None:
        self.retry_attempts = max(1, retry_attempts)
        # 默认每引擎独立注册表（同引擎内跨 run 累积断路状态，且保证测试隔离）
        self.breakers = breakers or BreakerRegistry()

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
        tool_class = classify_tool(tool_name)

        # ── Stage 0: 工具存在性 ──
        if not self._tool_exists(tool_name, tools):
            msg = self._unknown_tool_msg(tool_name, tools)
            if tracker:
                tracker.record(tool_name, params, False, msg, error=msg)
            return ToolExecuteResult(tool_name, False, msg, error=msg, tool_class=tool_class)

        # ── Stage 1: 标准化 ──
        try:
            params = ToolNode.normalize_params(params)
        except Exception as e:  # noqa: BLE001
            msg = f"参数标准化失败: {e}"
            return ToolExecuteResult(tool_name, False, msg, error=msg, tool_class=tool_class)

        logger.debug(f"执行工具: {tool_name}, 参数: {mask_sensitive_params(params)}")

        # ── Stage 2: 参数幻觉校验 ──
        ok, reason = validate_tool_params(params)
        if not ok:
            logger.warning(f"参数幻觉拦截: {tool_name} — {reason}")
            if tracker:
                tracker.record(tool_name, params, False, reason, error=reason)
            return ToolExecuteResult(tool_name, False, f"参数校验失败: {reason}", error=reason, tool_class=tool_class)

        # ── Stage 3: 权限闸门（SENSITIVE 暂不拦截，仅记录；可接 PermissionManager） ──
        if tool_class == "SENSITIVE":
            logger.debug(f"SENSITIVE 工具调用: {tool_name}")

        # ── Stage 4: 断路器 ──
        breaker = self.breakers.get(tool_name)
        if not breaker.allow():
            msg = f"工具 {tool_name} 断路器开启（连败熔断），已拒绝调用"
            logger.warning(msg)
            if tracker:
                tracker.record(tool_name, params, False, msg, error=msg)
            return ToolExecuteResult(tool_name, False, msg, error=msg, tool_class=tool_class)

        # ── Stage 5+6: 执行 + 重试 ──
        last_error: str | None = None
        attempts = 0
        raw: dict[str, Any] | None = None
        for attempt in range(1, self.retry_attempts + 1):
            attempts = attempt
            try:
                node = ToolNode(f"exec_{tool_name}", action_type=tool_name, **params)
                result = node.execute(context)
                raw = result
                if result.get("success", False):
                    breaker.record_success()
                    summary = _extract_summary(result)
                    if tracker:
                        tracker.record(tool_name, params, True, summary[:200])
                    return ToolExecuteResult(
                        tool_name, True, summary, tool_class=tool_class,
                        attempts=attempts, raw=raw,
                    )
                # 执行返回失败
                last_error = str(result.get("error") or result)
                breaker.record_failure()
                if is_terminal_error(last_error):
                    break  # 终端错误不重试
                logger.debug(f"工具 {tool_name} 第 {attempt} 次失败（瞬时）: {last_error[:120]}")
            except Exception as e:  # noqa: BLE001 — 单次执行异常归为失败
                last_error = f"{type(e).__name__}: {e}"
                breaker.record_failure()
                logger.error(f"工具 {tool_name} 执行异常: {e}")
                if is_terminal_error(last_error):
                    break

        # 全部重试失败
        obs = f"工具执行失败: {last_error}"
        if tracker:
            tracker.record(tool_name, params, False, obs, error=str(last_error))
        return ToolExecuteResult(
            tool_name, False, obs, error=str(last_error),
            tool_class=tool_class, attempts=attempts, raw=raw,
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
