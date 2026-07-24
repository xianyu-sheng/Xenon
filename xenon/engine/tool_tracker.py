"""
ToolExecutionTracker — 工具执行追踪器。

解决核心问题：LLM 声称执行了操作但实际没有。
追踪每次工具调用，提供验证能力，让引擎可以确认工具是否真的被调用过。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """单次工具调用记录。"""
    tool_name: str
    params: dict[str, Any]
    success: bool
    result_summary: str
    error: str | None = None
    state: str = ""
    attempts: int = 1
    elapsed_seconds: float = 0.0


@dataclass
class ToolExecutionTracker:
    """追踪工具执行状态，供引擎验证。

    用法:
        tracker = ToolExecutionTracker()
        tracker.record("write_file", {"file_path": "x.py"}, True, "写入 100 字节")
        tracker.has_executions()           # True
        tracker.execution_summary()        # "已执行 1 次工具调用: write_file(成功)"
    """

    calls: list[ToolCall] = field(default_factory=list)

    def record(
        self,
        tool_name: str,
        params: dict[str, Any],
        success: bool,
        result_summary: str = "",
        error: str | None = None,
        state: str = "",
        attempts: int = 1,
        elapsed_seconds: float = 0.0,
    ) -> None:
        """记录一次工具调用。"""
        call = ToolCall(
            tool_name=tool_name,
            params=params,
            success=success,
            result_summary=result_summary[:500],
            error=error,
            state=state or ("succeeded" if success else "failed"),
            attempts=max(0, attempts),
            elapsed_seconds=max(0.0, elapsed_seconds),
        )
        self.calls.append(call)
        status = "成功" if success else "失败"
        logger.info(f"[Tracker] {tool_name} -> {status}: {result_summary[:100]}")

    def has_executions(self) -> bool:
        """是否有任何工具被实际执行过。"""
        return len(self.calls) > 0

    def has_successful_executions(self) -> bool:
        """是否有成功执行的工具调用。"""
        return any(c.success for c in self.calls)

    def successful_tools(self) -> list[str]:
        """返回成功执行的工具名称列表。"""
        return [c.tool_name for c in self.calls if c.success]

    def failed_tools(self) -> list[str]:
        """返回失败的工具名称列表。"""
        return [c.tool_name for c in self.calls if not c.success]

    def consecutive_failures(self) -> int:
        """Return the number of failures since the most recent success."""
        count = 0
        for call in reversed(self.calls):
            if call.success:
                break
            count += 1
        return count

    def execution_summary(self) -> str:
        """生成人类可读的执行摘要。"""
        if not self.calls:
            return "未执行任何工具调用"

        total = len(self.calls)
        success_count = sum(1 for c in self.calls if c.success)
        fail_count = total - success_count

        parts = [f"已执行 {total} 次工具调用"]
        if success_count:
            tools = ", ".join(self.successful_tools())
            parts.append(f"成功: {success_count} 次 ({tools})")
        if fail_count:
            failed = ", ".join(self.failed_tools())
            parts.append(f"失败: {fail_count} 次 ({failed})")

        return "; ".join(parts)

    def detail_log(self) -> str:
        """生成详细的执行日志，用于注入 LLM 上下文。"""
        if not self.calls:
            return "(无工具执行记录)"

        lines = []
        for i, call in enumerate(self.calls, 1):
            status = "[OK]" if call.success else "[FAIL]"
            line = f"{i}. {status} {call.tool_name}"
            if call.params:
                # 只显示关键参数
                key_params = {}
                for k in ("file_path", "action", "url", "git_command"):
                    if k in call.params:
                        key_params[k] = str(call.params[k])[:80]
                if key_params:
                    line += f"({key_params})"
            if call.result_summary:
                line += f" -> {call.result_summary[:150]}"
            if call.error:
                line += f" [错误: {call.error[:100]}]"
            lines.append(line)

        return "\n".join(lines)

    def get_history(self) -> list[dict[str, Any]]:
        """返回执行历史的字典列表。"""
        return [
            {
                "tool": c.tool_name,
                "params": c.params,
                "success": c.success,
                "result": c.result_summary,
                "error": c.error,
                "state": c.state,
                "attempts": c.attempts,
                "elapsed_seconds": c.elapsed_seconds,
            }
            for c in self.calls
        ]

    def reset(self) -> None:
        """清空追踪记录（每轮对话开始时调用）。"""
        self.calls.clear()
