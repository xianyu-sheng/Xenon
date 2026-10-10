"""Tool classification and execution levels (extracted from tool_executor).

Single source for tool → sensitivity / required-execution-level mapping and
the execution-policy hard-denial text.
"""

from __future__ import annotations

import re
from typing import Any

from xenon.engine.context import AgentContext
from xenon.nodes.tool_node import _DYNAMIC_TOOLS
from xenon.nodes.tool_registry import BUILTIN_TOOL_REGISTRY

_SENSITIVE_TOOLS = {
    "command",
    "spawn_agent",
}  # 任意 shell 执行 + 子 Agent 委派——最高风险
_WRITE_TOOLS = {
    "write_file",
    "edit_file",
    "create_directory",
    "batch_write",
    "batch_edit",
    "edit_with_llm",
    "append_file",
    "git",
    "refactor",
    "register_tool",
    "clone_repo",
}
# 其余按 INFO 处理（read_file/list_files/search_files/web_fetch/github_fetch...）

# 参数幻觉校验豁免白名单：这些参数合法持有长文本/代码，不参与结构性检查
_TOOL_CONTENT_PARAMS = frozenset(
    {
        "content",
        "old_text",
        "new_text",
        "code",
        "text",
        "diff",
        "patch",
        "replacement",
        "snippet",
    }
)


def classify_tool(
    tool_name: str,
    params: dict[str, Any] | None = None,
) -> str:
    """返回 INFO | WRITE | SENSITIVE，必要时细分 MCP 远端能力。

    查询优先级：
    1. mcp_call 特判（远端工具名按语义判断）
    2. BUILTIN_TOOL_REGISTRY 的 risk 字段（涵盖内置工具与所有 register_tool_handler 注册的插件）
    3. _DYNAMIC_TOOLS（LLM 运行时通过 register_tool 元工具注册）→ SENSITIVE
    4. 硬编码集合兜底（迁移阶段残留）
    5. 默认 SENSITIVE（「未知即从严」，与 MCP 未知工具一致）
    """
    if tool_name == "mcp_call" and params:
        remote_name = str(params.get("tool_name", ""))
        if _EXECUTING_MCP_NAME.search(remote_name):
            return "SENSITIVE"
        if _MUTATING_MCP_NAME.search(remote_name):
            return "WRITE"
        if _READ_ONLY_MCP_NAME.search(remote_name):
            return "INFO"
        return "SENSITIVE"
    if tool_name == "mcp_call":
        return "SENSITIVE"
    # 优先查注册表 risk 字段
    defn = BUILTIN_TOOL_REGISTRY.get(tool_name)
    if defn is not None:
        return defn.risk
    # LLM 运行时注册的动态工具
    if tool_name in _DYNAMIC_TOOLS:
        return "SENSITIVE"
    # 硬编码集合兜底（迁移期）
    if tool_name in _SENSITIVE_TOOLS:
        return "SENSITIVE"
    if tool_name in _WRITE_TOOLS:
        return "WRITE"
    # 未知工具从严：不假设只读
    return "SENSITIVE"


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
    # 查注册表 risk 字段（优先级高于旧的硬编码集合）
    defn = BUILTIN_TOOL_REGISTRY.get(tool_name)
    if defn is not None:
        return {"INFO": 1, "WRITE": 2, "SENSITIVE": 3}.get(defn.risk, 3)
    # 未知工具从严：不假设只读（同 MCP 未知远端工具）
    return 3


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
