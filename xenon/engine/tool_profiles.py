"""Model-facing tool profiles.

Handlers remain available for compatibility and for internal adapters; a
profile only controls what the model sees.  ``legacy`` preserves the current
surface, while ``core`` exposes the small coding surface proposed in #190.
"""

from __future__ import annotations

import os
import re
from typing import Any

CORE_TOOL_NAMES = frozenset(
    {
        "read",
        "search",
        "apply_patch",
        "bash",
        "mcp_install",
        "mcp_verify",
        "mcp_call",
    }
)

# Names that are kept available to the engine internally even when the model
# only sees the core surface.  They are not injected into the tool schema.
ADAPTER_TOOL_NAMES = frozenset(
    {
        "run_tests",
        "git",
    }
)

# Harness/orchestration actions are not domain tools.  They must remain
# available even when the model-facing surface is core (plan approval and
# result lookup are not file/command authority).
ORCHESTRATION_TOOL_NAMES = frozenset(
    {
        "submit_plan",
        "update_plan",
        "tool_result_read",
    }
)

VALID_PROFILES = frozenset({"legacy", "core"})

# In core mode file changes go through apply_patch, not ad-hoc shell writes.
# This keeps changed_paths/atomic rollback available to TaskState.
_CORE_SHELL_WRITE = re.compile(
    r"(?i)(?:\bset-content\b|\badd-content\b|\bout-file\b|\bnew-item\b|"
    r"\bremove-item\b|\bmove-item\b|\bcopy-item\b|\brename-item\b|"
    r"\btee\b|\btouch\b|\bsed\s+-i\b|(?:^|[\s;&|])(?:cat|echo|printf)\s+[^\n;|]*>)"
)


def resolve_tool_profile(value: str | None = None) -> str:
    """Resolve the profile once per engine construction (env/config friendly)."""

    candidate = (value or os.environ.get("XENON_TOOL_PROFILE", "")).strip().lower()
    return candidate if candidate in VALID_PROFILES else "legacy"


def filter_tools_for_profile(
    tools: dict[str, dict[str, Any]], profile: str | None = None
) -> dict[str, dict[str, Any]]:
    selected = resolve_tool_profile(profile)
    if selected != "core":
        return dict(tools)
    return {name: schema for name, schema in tools.items() if name in CORE_TOOL_NAMES}


def set_tool_profile(context: Any, profile: str | None = None) -> str:
    selected = resolve_tool_profile(profile)
    if context is not None and hasattr(context, "set"):
        context.set("_tool_profile", selected)
    return selected


def profile_tool_guard(
    tool_name: str, context: Any, params: dict[str, Any] | None = None
) -> str | None:
    """Deny legacy domain tools when the active profile is ``core``.

    Internal orchestration (plan submission, plan updates, result lookup) is
    allowed because it carries no file/command side effect.
    """

    if context is None or not hasattr(context, "get"):
        return None
    profile = str(context.get("_tool_profile") or "legacy").lower()
    if profile != "core":
        return None
    allowed = CORE_TOOL_NAMES | ORCHESTRATION_TOOL_NAMES
    if tool_name in allowed:
        if tool_name == "bash":
            command = str(
                (params or {}).get("action")
                or (params or {}).get("command")
                or ""
            )
            if _CORE_SHELL_WRITE.search(command):
                return "core profile 要求文件修改走 apply_patch，不要用 shell 写文件。"
        return None
    return (
        f"当前工具面为 core，不暴露 {tool_name}。"
        "请使用 read/search/apply_patch/bash/mcp_*；"
        "测试和 git 请通过 bash 的受控适配器执行。"
    )
