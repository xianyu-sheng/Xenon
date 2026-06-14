"""
ToolNode — 工具执行调度节点。

通过 ToolRegistry 将工具调用分发到独立的工具类（builtin/*.py）。
本模块仅保留: 调度逻辑、参数规范化、动态工具注册表、安全异常类。
所有具体工具实现已迁移到 tools/builtin/*.py。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from omniagent.engine.context import AgentContext
from omniagent.nodes.base import BaseNode
from omniagent.tools.security import PARAM_ALIASES as _PARAM_ALIASES

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 动态工具注册表
# ═══════════════════════════════════════════════════════════════

_DYNAMIC_TOOLS: dict[str, dict] = {}


def register_dynamic_tool(name: str, handler, description: str, params: dict) -> None:
    """注册一个动态工具，之后可通过 ToolNode(action_type=name) 调用。"""
    _DYNAMIC_TOOLS[name] = {
        "handler": handler,
        "description": description,
        "params": params,
    }
    logger.debug(f"[DynamicTool] 注册工具: {name}")


def get_dynamic_tool_schema(name: str) -> dict | None:
    """获取动态工具的描述（用于注入到 LLM 工具列表）。"""
    info = _DYNAMIC_TOOLS.get(name)
    if not info:
        return None
    return {"name": name, "description": info["description"], "params": info["params"]}


def list_dynamic_tools() -> list[str]:
    """列出所有已注册的动态工具名。"""
    return list(_DYNAMIC_TOOLS.keys())


# ═══════════════════════════════════════════════════════════════
# 安全异常
# ═══════════════════════════════════════════════════════════════

class SecurityError(Exception):
    """安全策略违规异常。"""


# ═══════════════════════════════════════════════════════════════
# ToolNode — 工具调度节点
# ═══════════════════════════════════════════════════════════════

class ToolNode(BaseNode):
    """工具执行调度节点 — 通过 ToolRegistry 分发到独立工具类。

    执行流程:
    1. ToolRegistry.execute_sync() → 调度到 builtin/*.py 工具类 (主要路径)
    2. _DYNAMIC_TOOLS → 运行时注册的自定义工具 (回退路径)
    3. ValueError → 未知工具

    外部依赖:
    - ToolNode.normalize_params() — ToolExecutor + 各引擎的参数规范化
    - ToolNode.set_approval_handler() — REPL 注入交互式审批回调
    - SecurityError — shell_runner 捕获
    - _DYNAMIC_TOOLS — react_engine / async_engine 查询动态工具
    """

    # ── 参数别名映射 — 从 tools.security 共享模块导入 ──
    # （由模块级 `from omniagent.tools.security import PARAM_ALIASES as _PARAM_ALIASES` 提供）
    # 类级别的审批处理器（由 REPL 注入）
    _approval_handler: Callable | None = None

    def __init__(
        self,
        node_id: str,
        *,
        action_type: str = "command",
        action: str = "",
        output_slot: str | None = None,
        default_next: str | None = None,
        security_enabled: bool = True,
        timeout: int = 60,
        cwd: str | None = None,
        encoding: str = "utf-8",
        **kwargs,
    ) -> None:
        super().__init__(node_id, output_slot=output_slot, default_next=default_next)
        self.action_type = action_type
        self.action = action
        self.security_enabled = security_enabled
        self.timeout = timeout
        self.cwd = cwd
        self.encoding = encoding
        self.output_slot = output_slot
        self.default_next = default_next
        # 存储 LLM 传递的所有额外参数（file_path, content, url 等）
        self._extra_params: dict = kwargs

    # ── 类方法 ──────────────────────────────────────────────

    @classmethod
    def set_approval_handler(cls, handler: Callable | None) -> None:
        """设置交互式审批处理器。handler(tool_name, params_preview) -> bool"""
        cls._approval_handler = handler

    @classmethod
    def normalize_params(cls, params: dict, *, action_type: str = "") -> dict:
        """将 LLM 常用的参数别名映射为标准参数名。

        不再过滤未知参数 — 之前基于 _VALID_PARAMS 白名单的过滤导致
        LLM 传递的有效参数（如 city）被静默丢弃。现在所有参数都透传，
        ToolNode.__init__() 的 **kwargs 会安全捕获未知参数。

        Args:
            params: LLM 返回的原始参数字典
            action_type: 工具类型（如 "list_files"），用于跳过冲突的别名

        例: {"path": ".", "query": "foo"} → {"file_path": ".", "search_pattern": "foo"}
        """
        result = dict(params)

        # 别名映射
        for std_name, aliases in _PARAM_ALIASES.items():
            if std_name in result:
                continue  # 标准名已存在，不覆盖
            for alias in aliases:
                if alias in result:
                    result[std_name] = result.pop(alias)
                    break

        return result

    # ── 执行 ────────────────────────────────────────────────

    def execute(self, context: AgentContext) -> dict[str, Any]:
        """执行工具 — 通过 ToolRegistry 调度到独立工具类。

        调度优先级:
        1. ToolRegistry.execute_sync() — 调度到 builtin/*.py 工具类（24 个内置工具）
        2. _DYNAMIC_TOOLS — 运行时注册的自定义工具
        3. ValueError — 未知工具
        """
        # ── 收集工具参数 ──
        tool_params = dict(self._extra_params)
        # 合并显式属性（优先于 _extra_params 中的同名键）
        for attr in (
            "action", "file_path", "content", "search_pattern", "pattern",
            "url", "git_command", "old_text", "new_text", "source", "destination",
            "symbol", "query", "refactor_action", "old_name", "new_name",
            "tool_name", "tool_args", "repo", "github_action", "github_path",
            "branch", "city", "lang", "description", "python_function",
            "command_template", "params", "files", "edits", "mcp_server",
            "max_depth", "file_filter", "append",
        ):
            val = getattr(self, attr, None)
            if val is not None and val != "" and val != [] and val != {}:
                tool_params.setdefault(attr, val)

        # ── 路径 1: ToolRegistry（主要路径，覆盖 24 个内置工具）──
        try:
            from omniagent.tools.registry import get_registry
            registry = get_registry()
            if registry.has(self.action_type):
                return registry.execute_sync(
                    self.action_type, tool_params, context,
                    security_enabled=self.security_enabled,
                    cwd=self.cwd, encoding=self.encoding, timeout=self.timeout,
                    output_slot=self.output_slot,
                )
        except (ImportError, ValueError) as e:
            logger.debug(f"ToolRegistry 调度失败: {e}")

        # ── 路径 2: 动态工具（运行时注册的自定义工具）──
        dynamic = _DYNAMIC_TOOLS.get(self.action_type)
        if dynamic:
            try:
                result = dynamic["handler"](context)
                return result if isinstance(result, dict) else {
                    "action_type": self.action_type, "success": True,
                    "content": str(result),
                }
            except Exception as e:
                return {
                    "action_type": self.action_type, "success": False,
                    "error": str(e),
                }

        # ── 未知工具 ──
        raise ValueError(
            f"未知工具: '{self.action_type}'，"
            f"可用内置: [可通过 ToolRegistry.list_names() 查询]，"
            f"可用动态: {list(_DYNAMIC_TOOLS.keys())}"
        )
