"""工具执行断路器 — 防止重复失败的工具消耗 token。

当同一工具连续失败达到阈值时，暂时将该工具列入冷却名单，
跳过后面的调用请求，避免 LLM 进入无意义的反复重试。

与工具失败重试结合使用: 先重试 1 次，仍失败则计数并检查断路器。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CircuitState:
    """单个工具的断路器状态。"""

    name: str
    consecutive_failures: int = 0
    total_failures: int = 0
    cooldown_until: float = 0.0  # Unix timestamp
    last_error: str = ""


class CircuitBreaker:
    """工具执行断路器。

    规则:
    - 终端错误（文件不存在/权限拒绝/路径越界）→ 立即断路，告知 Agent 换方案
    - 可重试错误（网络超时/临时故障）→ 连续失败 3 次后进入冷却期（默认 30 秒）
    - 冷却期结束后允许 1 次重试：
      - 成功 → 重置计数
      - 失败 → 延长冷却期（翻倍，最大 5 分钟）

    使用方式:
        breaker = CircuitBreaker()

        if not breaker.allow("some_tool"):
            return "工具暂时不可用，请稍后重试"

        try:
            result = execute_tool()
            breaker.on_success("some_tool")
        except Exception as e:
            breaker.on_failure("some_tool", str(e))
            # 可选: 重试 1 次
    """

    # ── 终端错误模式：匹配这些模式的错误不会通过重试解决 ──
    _TERMINAL_ERROR_PATTERNS: list[tuple[str, str]] = [
        # (工具名模式, 错误消息正则)
        ("read_file", r"(?:文件不存在|file\s+not\s+found|no\s+such\s+file|无法找到|找不到)"),
        ("list_files", r"(?:目录不存在|not\s+a\s+directory|no\s+such\s+directory|无法找到|找不到)"),
        ("write_file", r"(?:路径越界|permission\s+denied|权限|access\s+is\s+denied|不在项目目录)"),
        ("edit_file", r"(?:路径越界|permission\s+denied|权限|access\s+is\s+denied|不在项目目录|精确匹配.*失败|原文.*找不到)"),
        ("file_move", r"(?:路径越界|permission\s+denied|权限|源文件不存在|source.*not\s+found)"),
        ("file_copy", r"(?:路径越界|permission\s+denied|权限|源文件不存在|source.*not\s+found)"),
        ("create_directory", r"(?:路径越界|permission\s+denied|权限|已存在.*文件|already\s+exists.*file)"),
        (".*", r"(?:permission\s+denied|access\s+denied|权限不足|路径越界)"),
    ]

    def __init__(
        self,
        failure_threshold: int = 3,
        base_cooldown: float = 30.0,  # 秒
        max_cooldown: float = 300.0,  # 最大冷却 5 分钟
    ) -> None:
        self._states: dict[str, CircuitState] = {}
        self.failure_threshold = failure_threshold
        self.base_cooldown = base_cooldown
        self.max_cooldown = max_cooldown

    @classmethod
    def is_terminal_error(cls, tool_name: str, error_msg: str) -> bool:
        """判断错误是否是终端错误（重试无意义）。

        终端错误特征：
        - 文件不存在 → 无论重试多少次都不会出现
        - 权限拒绝 → 重试不会改变权限
        - 路径越界 → 安全限制，不会改变
        """
        import re
        error_lower = error_msg.lower()
        for tool_pattern, error_pattern in cls._TERMINAL_ERROR_PATTERNS:
            if re.match(tool_pattern, tool_name):
                if re.search(error_pattern, error_lower):
                    return True
        return False

    def allow(self, tool_name: str) -> bool:
        """检查工具是否允许执行。

        如果工具在冷却期内，返回 False 并附带原因。
        """
        state = self._states.get(tool_name)
        if state is None:
            return True

        if state.cooldown_until > time.time():
            remaining = int(state.cooldown_until - time.time())
            logger.debug(
                f"断路器阻止 {tool_name}: 冷却中 ({remaining}s 剩余), "
                f"原因: {state.last_error[:100]}"
            )
            return False

        # 冷却期已过，允许重试
        logger.debug(f"断路器允许 {tool_name} 重试 (冷却期已过)")
        return True

    def on_success(self, tool_name: str) -> None:
        """工具执行成功 — 重置计数器。"""
        if tool_name in self._states:
            logger.info(f"断路器: {tool_name} 恢复 (该工具之前有 {self._states[tool_name].consecutive_failures} 次连续失败)")
            del self._states[tool_name]

    def on_failure(self, tool_name: str, error: str) -> None:
        """工具执行失败 — 增加计数器。"""
        state = self._states.get(tool_name)
        if state is None:
            state = CircuitState(name=tool_name)
            self._states[tool_name] = state

        state.consecutive_failures += 1
        state.total_failures += 1
        state.last_error = error

        if state.consecutive_failures >= self.failure_threshold:
            # 进入冷却期
            cooldown = min(
                self.base_cooldown * (2 ** (state.consecutive_failures - self.failure_threshold)),
                self.max_cooldown,
            )
            state.cooldown_until = time.time() + cooldown
            logger.warning(
                f"断路器: {tool_name} 进入冷却 ({cooldown:.0f}s), "
                f"连续失败 {state.consecutive_failures} 次, 错误: {error[:100]}"
            )

    def on_failure_cooldown(self, tool_name: str, error: str) -> str | None:
        """记录失败并返回冷却消息（如果触发）。

        便捷方法: 结合了 on_failure + allow 检查。

        Returns:
            None 表示未触发断路器
            字符串表示断路器消息（应直接返回给 Agent）
        """
        self.on_failure(tool_name, error)

        if not self.allow(tool_name):
            state = self._states.get(tool_name)
            if state is None:
                return None
            remaining = int(state.cooldown_until - time.time())
            return (
                f"⚠️ 工具 '{tool_name}' 已连续失败 {state.consecutive_failures} 次，"
                f"暂时不可用（冷却 {remaining}s）。最后的错误: {error[:200]}\n"
                f"请尝试其他方法完成任务。"
            )
        return None

    def status(self, tool_name: str | None = None) -> dict[str, Any]:
        """查询断路器状态。

        不传 tool_name 则返回所有被断路工具的状态。
        """
        if tool_name:
            state = self._states.get(tool_name)
            if state is None:
                return {"name": tool_name, "tripped": False}
            return {
                "name": tool_name,
                "tripped": state.cooldown_until > time.time(),
                "consecutive_failures": state.consecutive_failures,
                "total_failures": state.total_failures,
                "cooldown_remaining": max(0, int(state.cooldown_until - time.time())),
                "last_error": state.last_error[:200],
            }

        tripped = []
        for name, state in self._states.items():
            if state.cooldown_until > time.time():
                tripped.append({
                    "name": name,
                    "consecutive_failures": state.consecutive_failures,
                    "cooldown_remaining": max(0, int(state.cooldown_until - time.time())),
                    "last_error": state.last_error[:100],
                })
        return {"tripped_count": len(tripped), "tripped": tripped}

    def reset(self, tool_name: str | None = None) -> None:
        """重置断路器状态。

        Args:
            tool_name: 要重置的工具名，None 表示重置全部。
        """
        if tool_name:
            self._states.pop(tool_name, None)
        else:
            self._states.clear()
