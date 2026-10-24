"""Small, deterministic recipes for atomic tasks.

The general ReAct loop is appropriate for open-ended coding.  It is a poor
fit for bounded configuration/install operations: those should have a known
stage list, a small allowed-tool set, and a hard scope guard.  This module
stores recipes in AgentContext (not a second task store) and is intentionally
small enough to remain inspectable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TaskRecipe:
    id: str
    stages: tuple[str, ...]
    allowed_tools: frozenset[str]
    forbidden_effects: frozenset[str]
    soft_tool_budget: int
    prompt: str
    engine: str = "react"


MCP_INSTALL = TaskRecipe(
    id="mcp.install",
    stages=(
        "resolve_target",
        "write_config",
        "connect_and_discover",
        "verify_one_read_only_call",
    ),
    allowed_tools=frozenset(
        {
            "mcp_install",
            "mcp_verify",
            # Plan bookkeeping is allowed; it has no side effects.
            "update_plan",
            "tool_result_read",
        }
    ),
    forbidden_effects=frozenset(
        {
            "repo_write",
            "project_test_run",
            "git_commit",
            "git_push",
            "source_inspection",
        }
    ),
    soft_tool_budget=8,
    prompt=(
        "## 原子任务配方：MCP 安装\n"
        "这轮只允许按固定步骤完成：用 `mcp_install` 写配置并发现工具，"
        "然后用 `mcp_verify` 调一个只读工具验证。\n"
        "禁止：读取 Xenon 源码、改仓库文件、跑项目测试、git commit/push、"
        "写临时脚本。不要探索 workspace；`mcp_install` 已经处理配置和连接。\n"
        "完成后用一条短结果说明：工具数、验证结果、配置路径。"
    ),
)


_MCP_WORD = re.compile(r"\bmcp\b", re.IGNORECASE)
_INSTALL_VERB = re.compile(
    r"(?:安装|装好|装一下|添加|新增|配置|接入|install|add|setup|configure)",
    re.IGNORECASE,
)
_QUERY_ONLY = re.compile(
    r"(?:怎么|如何|什么|哪些|步骤|教程|命令|文档|介绍|explain|how\s+to|what|guide)",
    re.IGNORECASE,
)
_ACTION_CUE = re.compile(
    r"(?:请你|帮我|给我|直接|进行|执行|开始|please|go\s+ahead|do\s+it)",
    re.IGNORECASE,
)


def recipe_for_request(text: str, *, level: int = 0) -> TaskRecipe | None:
    """Return an atomic recipe only for an authorized, action-shaped request."""

    value = (text or "").strip()
    if not value or not _MCP_WORD.search(value) or not _INSTALL_VERB.search(value):
        return None
    # "怎么安装 / 给一条安装命令" is a question, not authorization to act.
    if _QUERY_ONLY.search(value) and not _ACTION_CUE.search(value):
        return None
    # Do not activate recipes for a read-only/answer-only turn.
    if int(level) < 2:
        return None
    return MCP_INSTALL


def activate_recipe(
    context: Any, text: str, *, level: int = 0
) -> TaskRecipe | None:
    """Attach a recipe to this turn; return it for prompt construction."""

    recipe = recipe_for_request(text, level=level)
    if recipe is None:
        # A recipe is turn-scoped.  Never let an install/configuration guard
        # leak into the next unrelated coding turn.
        context.set("_task_recipe", None)
        return None
    context.set("_task_recipe", {
        "id": recipe.id,
        "stages": list(recipe.stages),
        "allowed_tools": sorted(recipe.allowed_tools),
        "forbidden_effects": sorted(recipe.forbidden_effects),
        "soft_tool_budget": recipe.soft_tool_budget,
        "tool_calls": 0,
    })
    return recipe


def active_recipe(context: Any) -> dict[str, Any] | None:
    recipe = context.get("_task_recipe") if context is not None else None
    return recipe if isinstance(recipe, dict) else None


def recipe_allowed_tools(context: Any) -> frozenset[str] | None:
    recipe = active_recipe(context)
    if recipe is None:
        return None
    allowed = recipe.get("allowed_tools")
    if not isinstance(allowed, (list, tuple, set)):
        return frozenset()
    return frozenset(str(item) for item in allowed)


def recipe_tool_guard(
    tool_name: str,
    params: dict[str, Any],
    context: Any,
) -> str | None:
    """Return a deterministic denial when a tool is outside the recipe.

    This is intentionally called by ToolExecutor, so it covers ReAct, Plan-
    Execute, direct tool calls and future engines with one boundary.
    """

    recipe = active_recipe(context)
    if recipe is None:
        return None
    allowed = recipe_allowed_tools(context) or frozenset()
    calls = int(recipe.get("tool_calls", 0) or 0) + 1
    recipe["tool_calls"] = calls
    if tool_name not in allowed:
        return (
            f"原子任务 {recipe.get('id')} 不允许工具 {tool_name}。"
            "请使用 mcp_install 写配置/发现工具，再用 mcp_verify 做只读验证；"
            "不要读取源码、改仓库、跑项目测试或 git commit。"
        )
    budget = int(recipe.get("soft_tool_budget", 8) or 8)
    if calls > budget:
        return (
            f"原子任务 {recipe.get('id')} 已超过软预算 {budget} 次工具调用。"
            "停止继续尝试，直接报告已完成/未完成的真实状态。"
        )
    return None


def recipe_prompt(context: Any) -> str:
    recipe = active_recipe(context)
    if recipe is None:
        return ""
    recipe_id = str(recipe.get("id") or "")
    if recipe_id == MCP_INSTALL.id:
        return MCP_INSTALL.prompt
    stages = ", ".join(str(item) for item in recipe.get("stages") or ())
    return f"## 原子任务配方：{recipe_id}\n只允许按步骤执行：{stages}。"


def clear_recipe(context: Any) -> None:
    if context is not None:
        context.set("_task_recipe", None)
