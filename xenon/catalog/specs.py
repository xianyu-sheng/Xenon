"""Tool specs and packs.

The kernel sees capabilities and effects, not handler names.  This module is the
only place that maps a concrete tool name to those semantics.
"""

from __future__ import annotations

from xenon.kernel.contracts import Capability, EffectKind, Risk, ToolSpec

CORE_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="read",
        pack="core",
        capabilities=frozenset({Capability.READ}),
        effects=frozenset({EffectKind.READ}),
        risk=Risk.INFO,
        params_schema={"file_path": "path to read"},
        description="Read a file from the workspace.",
        preferences={"read": 20},
    ),
    ToolSpec(
        name="search",
        pack="core",
        capabilities=frozenset({Capability.SEARCH}),
        effects=frozenset(),
        risk=Risk.INFO,
        params_schema={"pattern": "text or regex", "path": "optional root"},
        description="Search the workspace for a pattern.",
        preferences={"search": 20},
    ),
    ToolSpec(
        name="apply_patch",
        pack="core",
        capabilities=frozenset({Capability.EDIT}),
        effects=frozenset({EffectKind.FS_WRITE}),
        risk=Risk.WRITE,
        params_schema={
            "files": "list of {path, content} for full writes",
            "edits": "list of {file_path, old_text, new_text}",
        },
        description="Apply a transactional file patch.",
        preferences={"edit": 20},
    ),
    ToolSpec(
        name="bash",
        pack="core",
        capabilities=frozenset(
            {Capability.SHELL, Capability.VERIFY, Capability.VCS}
        ),
        effects=frozenset(
            {
                EffectKind.OPAQUE,
                EffectKind.TEST_RUN,
                EffectKind.VCS_COMMIT,
                EffectKind.VCS_PUSH,
            }
        ),
        risk=Risk.SENSITIVE,
        params_schema={"command": "shell command"},
        description="Run a shell command and observe its effects.",
        preferences={"shell": 20, "verify": 15, "vcs": 15},
    ),
)

LEGACY_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="read_file",
        pack="legacy",
        capabilities=frozenset({Capability.READ}),
        effects=frozenset({EffectKind.READ}),
        risk=Risk.INFO,
        params_schema={"file_path": "path to read"},
        description="Legacy file read adapter.",
        preferences={"read": 10},
    ),
    ToolSpec(
        name="search_files",
        pack="legacy",
        capabilities=frozenset({Capability.SEARCH}),
        effects=frozenset(),
        risk=Risk.INFO,
        params_schema={"pattern": "text or regex"},
        description="Legacy search adapter.",
        preferences={"search": 10},
    ),
    ToolSpec(
        name="write_file",
        pack="legacy",
        capabilities=frozenset({Capability.EDIT}),
        effects=frozenset({EffectKind.FS_WRITE}),
        risk=Risk.WRITE,
        params_schema={"file_path": "path", "content": "text"},
        description="Legacy whole-file write adapter.",
        preferences={"edit": 5},
    ),
    ToolSpec(
        name="edit_file",
        pack="legacy",
        capabilities=frozenset({Capability.EDIT}),
        effects=frozenset({EffectKind.FS_WRITE}),
        risk=Risk.WRITE,
        params_schema={"file_path": "path", "old_text": "text", "new_text": "text"},
        description="Legacy string-edit adapter.",
        preferences={"edit": 4},
    ),
    ToolSpec(
        name="run_tests",
        pack="legacy",
        capabilities=frozenset({Capability.VERIFY}),
        effects=frozenset({EffectKind.TEST_RUN}),
        risk=Risk.INFO,
        params_schema={"test_paths": "targets", "test_framework": "auto"},
        description="Legacy structured test adapter.",
        preferences={"verify": 20},
    ),
    ToolSpec(
        name="git",
        pack="legacy",
        capabilities=frozenset({Capability.VCS}),
        effects=frozenset({EffectKind.VCS_COMMIT, EffectKind.VCS_PUSH}),
        risk=Risk.SENSITIVE,
        params_schema={"git_command": "commit/push/status..."},
        description="Legacy git adapter.",
        preferences={"vcs": 20},
    ),
    ToolSpec(
        name="command",
        pack="legacy",
        capabilities=frozenset({Capability.SHELL}),
        effects=frozenset({EffectKind.OPAQUE}),
        risk=Risk.SENSITIVE,
        params_schema={"command": "shell command"},
        description="Legacy shell adapter.",
        preferences={"shell": 10},
    ),
)

MCP_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="mcp_call",
        pack="mcp",
        capabilities=frozenset({Capability.REMOTE}),
        effects=frozenset({EffectKind.NETWORK, EffectKind.OPAQUE}),
        risk=Risk.SENSITIVE,
        params_schema={"server": "server name", "tool": "tool name", "arguments": "dict"},
        description="Call an MCP server tool.",
        preferences={"remote": 10},
    ),
)

ALL_SPECS: tuple[ToolSpec, ...] = CORE_SPECS + LEGACY_SPECS + MCP_SPECS
