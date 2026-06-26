"""
Test tools — pytest, run_test

Builtin sync wrappers for test execution tools.
Delegates to the async BaseTool implementations in omniagent.tools.test_runner.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from omniagent.engine.context import AgentContext
from omniagent.tools.builtin.base import BaseTool

logger = logging.getLogger(__name__)


class PytestTool(BaseTool):
    """运行 pytest 测试框架。"""

    name = "pytest"

    def execute(self, context: AgentContext) -> dict[str, Any]:
        test_path = self._extra.get("test_path", "tests/")
        filter_expr = self._extra.get("filter_expr", "")
        stop_on_fail = self._extra.get("stop_on_fail", False)

        try:
            from omniagent.tools.test_runner import PytestTool as _Pytest

            tool = _Pytest()
            params: dict[str, Any] = {"test_path": test_path}
            if filter_expr:
                params["filter_expr"] = filter_expr
            if stop_on_fail:
                params["stop_on_fail"] = stop_on_fail

            result = asyncio.run(tool.invoke(params))
            return {
                "action_type": "pytest",
                "success": not result.is_error,
                "content": result.content,
                "error": result.content if result.is_error else None,
            }
        except Exception as e:
            logger.error("pytest 工具执行失败: %s", e)
            return {"action_type": "pytest", "success": False, "error": str(e)}


class RunTestTool(BaseTool):
    """执行任意测试命令（pytest 以外的框架）。"""

    name = "run_test"

    def execute(self, context: AgentContext) -> dict[str, Any]:
        command = self._extra.get("command", "")
        timeout_seconds = self._extra.get("timeout_seconds", 120)

        if not command:
            return {"action_type": "run_test", "success": False, "error": "需要 command 参数"}

        try:
            from omniagent.tools.test_runner import TestCommandTool as _RunTest

            tool = _RunTest()
            params: dict[str, Any] = {
                "command": command,
                "timeout_seconds": timeout_seconds,
            }

            result = asyncio.run(tool.invoke(params))
            return {
                "action_type": "run_test",
                "success": not result.is_error,
                "content": result.content,
                "error": result.content if result.is_error else None,
            }
        except Exception as e:
            logger.error("run_test 工具执行失败: %s", e)
            return {"action_type": "run_test", "success": False, "error": str(e)}
