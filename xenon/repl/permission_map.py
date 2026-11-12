"""Tool names to kernel capabilities.

The kernel speaks capabilities and must not know concrete tool names (that is
what `scripts/check_kernel_purity.py` enforces).  The approval panel, however,
receives tool names — so the translation lives here, in the front-end.
"""

from __future__ import annotations

from xenon.kernel.contracts import Capability

__all__ = ["capability_for_tool"]

_TOOL_CAPABILITIES = {
    "command": Capability.SHELL,
    "bash": Capability.SHELL,
    "run_script": Capability.SHELL,
    "run_tests": Capability.VERIFY,
    # The kernel catalog names its verify tool ``test``; without this entry the
    # panel's "turn" answer for a test approval built no grant and yet told the
    # user "本轮内不再询问".
    "test": Capability.VERIFY,
    "apply_patch": Capability.EDIT,
    "write_file": Capability.EDIT,
    "edit_file": Capability.EDIT,
    "append_file": Capability.EDIT,
    "batch_write": Capability.EDIT,
    "batch_edit": Capability.EDIT,
    "read_file": Capability.READ,
    "read_document": Capability.READ,
    "tool_result_read": Capability.READ,
    "list_files": Capability.READ,
    "search_files": Capability.SEARCH,
    "find_tools": Capability.DISCOVER,
    "git": Capability.VCS,
    "mcp_call": Capability.REMOTE,
}


def capability_for_tool(tool_name: str) -> Capability | None:
    """Map a prompt-visible tool name onto the kernel's capability vocabulary."""

    name = str(tool_name or "").strip().casefold()
    if name in _TOOL_CAPABILITIES:
        return _TOOL_CAPABILITIES[name]
    try:
        return Capability(name)
    except ValueError:
        return None
