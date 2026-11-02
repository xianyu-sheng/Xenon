"""Tool name -> capability mapping lives in the front-end, not the kernel.

`check_kernel_purity.py` forbids concrete tool names inside `xenon/kernel`, so
this mapping cannot live next to the policy.  That is a real constraint, not a
style preference: the kernel must speak capabilities only.
"""

from __future__ import annotations

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.kernel.contracts import Capability
from xenon.repl.permission_map import capability_for_tool


def test_known_tool_names_map_to_capabilities() -> None:
    assert capability_for_tool("command") is Capability.SHELL
    assert capability_for_tool("apply_patch") is Capability.EDIT
    assert capability_for_tool("run_tests") is Capability.VERIFY
    assert capability_for_tool("mcp_call") is Capability.REMOTE


def test_a_capability_name_maps_to_itself() -> None:
    assert capability_for_tool("verify") is Capability.VERIFY


def test_matching_is_case_insensitive() -> None:
    assert capability_for_tool("COMMAND") is Capability.SHELL


def test_an_unknown_tool_has_no_capability() -> None:
    assert capability_for_tool("nonsense") is None
    assert capability_for_tool("") is None
    assert capability_for_tool(None) is None  # type: ignore[arg-type]
