"""Model-facing tool profiles.

Handlers remain available for compatibility and for internal adapters; a
profile only controls what the model sees.  ``legacy`` preserves the current
surface, while ``core`` exposes the small coding surface proposed in #190.
"""

from __future__ import annotations

import os
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

VALID_PROFILES = frozenset({"legacy", "core"})


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
