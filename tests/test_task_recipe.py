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


def test_react_recipe_executes_mcp_install_without_repo_work(
    tmp_path, monkeypatch
):
    """Recipe + ReAct + ToolExecutor + real fake stdio server, end to end."""

    import json
    import sys

    from xenon.engine.react_engine import ReActEngine

    config = tmp_path / "credentials.yaml"
    config.write_text("deepseek: test-key\n", encoding="utf-8")
    monkeypatch.setattr(
        "xenon.repl.provider_registry._get_credentials_path", lambda: config
    )
    server = tmp_path / "fake_server.py"
    server.write_text(
        """import json, sys
for line in sys.stdin:
    req = json.loads(line)
    method, rid = req.get("method"), req.get("id")
    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "serverInfo": {"name": "fake", "version": "1"}}}), flush=True)
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": "echo", "description": "echo", "inputSchema": {"type": "object"}}
        ]}}), flush=True)
    elif method == "tools/call":
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": "ok"}], "isError": False}}), flush=True)
""",
        encoding="utf-8",
    )

    context = AgentContext({"_execution_level": 3})
    assert activate_recipe(context, "请你安装 MCP", level=3) is not None
    engine = ReActEngine(["fake/model"], max_iterations=4, native_fc=False)
    calls = {"n": 0}

    def fake_llm(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps(
                {
                    "thought": "install the fake MCP server",
                    "action": "mcp_install",
                    "action_input": {
                        "mcp_name": "fake",
                        "mcp_command": sys.executable,
                        "mcp_args": [str(server)],
                        "mcp_verify": True,
                    },
                }
            )
        return json.dumps({"final_answer": "installed fake with 1 tool"})

    engine._call_llm_for_phase = fake_llm  # type: ignore[method-assign]
    result = engine.run_task("请你安装 MCP", context=context)

    assert "installed fake" in result
    assert result.reason.value == "final"
    assert "name: fake" in config.read_text(encoding="utf-8")
    assert not (tmp_path / ".git").exists()


def test_recipe_is_turn_scoped_and_cleared_on_next_non_recipe_turn():
    context = AgentContext({})
    assert activate_recipe(context, "请你安装 MCP", level=3) is not None
    assert active_recipe(context) is not None

    assert activate_recipe(context, "解释一下这个函数", level=1) is None
    assert active_recipe(context) is None
