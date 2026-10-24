"""Atomic task recipes: deterministic scope guard, not a global step cap."""

from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.engine.task_recipe import (
    MCP_INSTALL,
    activate_recipe,
    active_recipe,
    recipe_allowed_tools,
    recipe_for_request,
    recipe_prompt,
    recipe_tool_guard,
)


def test_action_request_activates_mcp_install_recipe():
    recipe = recipe_for_request(
        "请你来进行安装 GitHub MCP", level=3
    )

    assert recipe is MCP_INSTALL
    assert "mcp_install" in recipe.allowed_tools
    assert "mcp_verify" in recipe.allowed_tools
    assert "git" not in recipe.allowed_tools


def test_question_request_does_not_activate_recipe():
    assert recipe_for_request("怎么安装 GitHub MCP？给一条命令", level=3) is None
    assert recipe_for_request("安装 MCP 需要哪些步骤？", level=3) is None
    assert recipe_for_request("请你安装 GitHub MCP", level=1) is None


def test_recipe_guard_denies_repo_and_source_work():
    context = AgentContext({})
    recipe = activate_recipe(context, "请你安装 GitHub MCP", level=3)
    assert recipe is not None

    denied = recipe_tool_guard("git", {"git_command": "status"}, context)
    assert denied is not None
    assert "mcp_install" in denied

    denied = recipe_tool_guard("read_file", {"file_path": "xenon/repl.py"}, context)
    assert denied is not None

    assert recipe_tool_guard("mcp_install", {"mcp_name": "github"}, context) is None
    assert recipe_tool_guard("mcp_verify", {"mcp_name": "github"}, context) is None


def test_recipe_guard_has_a_soft_budget():
    context = AgentContext({})
    assert activate_recipe(context, "install MCP", level=3) is not None

    for _ in range(MCP_INSTALL.soft_tool_budget):
        assert recipe_tool_guard("update_plan", {}, context) is None

    denied = recipe_tool_guard("update_plan", {}, context)
    assert denied is not None
    assert "软预算" in denied


def test_recipe_metadata_and_prompt_are_ledger_friendly():
    context = AgentContext({})
    assert activate_recipe(context, "please install MCP", level=3) is not None
    recipe = active_recipe(context)

    assert recipe is not None
    assert recipe["id"] == "mcp.install"
    assert recipe_allowed_tools(context) == MCP_INSTALL.allowed_tools
    assert "禁止" in recipe_prompt(context)
    assert "mcp_install" in recipe_prompt(context)
