"""Tool specs and packs.

The kernel sees capabilities and effects, not handler names.  This module is the
only place that maps a concrete tool name to those semantics.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

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
        params_schema={
            "tool_name": "MCP tool name, e.g. server:tool",
            "tool_args": "MCP tool argument object",
            "query": "optional result filter keyword",
            "max_chars": "optional result budget",
        },
        description=(
            "Call a configured MCP tool. Use tool_name (server:tool) and "
            "tool_args; approval is required."
        ),
        preferences={"remote": 10},
    ),
)

ALL_SPECS: tuple[ToolSpec, ...] = CORE_SPECS + LEGACY_SPECS + MCP_SPECS


_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "you",
        "use",
        "not",
        "all",
        "any",
        "are",
        "was",
        "has",
        "have",
        "can",
        "will",
        "into",
        "over",
        "via",
        "out",
        "its",
        "our",
        "your",
    }
)

_CJK_HINTS: tuple[tuple[str, str], ...] = (
    ("搜索", "search"),
    ("查询", "search"),
    ("查找", "search"),
    ("仓库", "repositor"),
    ("代码", "code"),
    ("问题", "issue"),
    ("创建", "create"),
    ("新建", "create"),
    ("列表", "list"),
    ("拉取", "pull"),
    ("请求", "pull"),
    ("用户", "user"),
    ("文件", "file"),
    ("提交", "commit"),
    ("评论", "comment"),
    ("分支", "branch"),
    ("星标", "star"),
)


def _text_tokens(value: str) -> set[str]:
    tokens = {
        item
        for item in re.findall(r"[a-z0-9]+", value.casefold())
        if len(item) >= 3 and item not in _STOPWORDS
    }
    for marker, token in _CJK_HINTS:
        if marker in value:
            tokens.add(token)
    return tokens


def _tokens_match(left: str, right: str) -> bool:
    if left == right:
        return True
    # Small, deterministic stemming for query/tool pairs such as
    # repository/repositories without pulling in a NLP dependency.
    return bool(
        len(left) >= 5
        and len(right) >= 5
        and (left.startswith(right[:5]) or right.startswith(left[:5]))
    )


def select_mcp_tool_specs(
    specs: Sequence[ToolSpec],
    objective: str,
    *,
    limit: int = 16,
    allowed: Sequence[str] = (),
) -> tuple[ToolSpec, ...]:
    """Choose a bounded, deterministic subset of MCP tool specs.

    An explicit allowlist wins.  Otherwise tool names are weighted more than
    descriptions; ties break by canonical name so the same objective and
    catalog always produce the same prompt.
    """

    cap = max(1, int(limit))
    ordered = tuple(sorted(specs, key=lambda item: item.name))
    allow = tuple(str(item).strip() for item in allowed if str(item).strip())
    if allow:
        by_name = {spec.name: spec for spec in ordered}
        return tuple(
            by_name[name]
            for name in allow[:cap]
            if name in by_name
        )

    objective_tokens = _text_tokens(objective)
    scored: list[tuple[int, str, ToolSpec]] = []
    for spec in ordered:
        # The server prefix (``github``) is shared by every tool on that
        # server and would otherwise give all candidates the same score.
        short_name = spec.name.split(":", 1)[-1]
        name_tokens = _text_tokens(short_name)
        description_tokens = _text_tokens(spec.description)
        score = 0
        for token in objective_tokens:
            if token in name_tokens:
                score += 12
            elif any(_tokens_match(token, item) for item in name_tokens):
                score += 8
            if token in description_tokens:
                score += 2
            elif any(_tokens_match(token, item) for item in description_tokens):
                score += 1
        scored.append((score, spec.name, spec))

    ranked = sorted(scored, key=lambda item: (-item[0], item[1]))
    selected = [spec for score, _name, spec in ranked if score > 0]
    if not selected:
        selected = [spec for _score, _name, spec in ranked]
    return tuple(selected[:cap])


def mcp_tool_specs(tools: Mapping[str, Any]) -> tuple[ToolSpec, ...]:
    """Build one catalog spec per discovered ``server:tool`` MCP name.

    ``MCPRegistry.tool_map`` also contains unique short aliases; only the
    canonical ``server:tool`` keys become specs.  The server process itself is
    still outside Xenon's filesystem sandbox; approval remains the boundary.
    """

    specs: list[ToolSpec] = []
    for global_name, entry in sorted(tools.items()):
        name = str(global_name)
        if ":" not in name:
            continue
        tool: Mapping[str, Any]
        if (
            isinstance(entry, tuple)
            and len(entry) == 2
            and isinstance(entry[1], Mapping)
        ):
            tool = entry[1]
        elif isinstance(entry, Mapping):
            tool = entry
        else:
            tool = {}
        schema = tool.get("inputSchema") or tool.get("input_schema") or {}
        if not isinstance(schema, Mapping):
            schema = {}
        description = str(tool.get("description") or "").strip()
        specs.append(
            ToolSpec(
                name=name,
                pack="mcp",
                capabilities=frozenset({Capability.REMOTE}),
                effects=frozenset({EffectKind.NETWORK, EffectKind.OPAQUE}),
                risk=Risk.SENSITIVE,
                params_schema=dict(schema),
                description=description[:500] or f"MCP tool {name}.",
                preferences={"remote": 100},
            )
        )
    return tuple(specs)
