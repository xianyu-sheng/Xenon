"""Deterministic MCP tool selection and prompt budget (#242)."""

from __future__ import annotations

from xenon.catalog.specs import mcp_tool_specs, select_mcp_tool_specs


def _specs():
    return mcp_tool_specs(
        {
            "github:search_repositories": (
                "github",
                {
                    "name": "search_repositories",
                    "description": "Search GitHub repositories by name or topic.",
                    "inputSchema": {"type": "object"},
                },
            ),
            "github:create_issue": (
                "github",
                {
                    "name": "create_issue",
                    "description": "Create a new issue in a repository.",
                    "inputSchema": {"type": "object"},
                },
            ),
            "github:list_issues": (
                "github",
                {
                    "name": "list_issues",
                    "description": "List issues in a repository.",
                    "inputSchema": {"type": "object"},
                },
            ),
            "weather:forecast": (
                "weather",
                {
                    "name": "forecast",
                    "description": "Get a weather forecast.",
                    "inputSchema": {"type": "object"},
                },
            ),
        }
    )


def test_selection_prefers_tool_name_tokens_and_is_stable() -> None:
    specs = _specs()
    first = select_mcp_tool_specs(
        specs, "search repositories for xianyu-sheng/Xenon", limit=2
    )
    second = select_mcp_tool_specs(
        specs, "search repositories for xianyu-sheng/Xenon", limit=2
    )
    assert [spec.name for spec in first] == [spec.name for spec in second]
    assert first[0].name == "github:search_repositories"
    assert len(first) == 2


def test_allowlist_wins_and_limit_is_respected() -> None:
    selected = select_mcp_tool_specs(
        _specs(),
        "search repositories",
        limit=2,
        allowed=("github:create_issue", "weather:forecast"),
    )
    assert [spec.name for spec in selected] == [
        "github:create_issue",
        "weather:forecast",
    ]
    limited = select_mcp_tool_specs(
        _specs(),
        "search repositories",
        limit=1,
        allowed=("github:create_issue", "weather:forecast"),
    )
    assert [spec.name for spec in limited] == ["github:create_issue"]


def test_no_match_still_returns_bounded_catalog() -> None:
    selected = select_mcp_tool_specs(
        _specs(), "completely unrelated words", limit=3
    )
    assert len(selected) == 3
    assert [spec.name for spec in selected] == sorted(
        spec.name for spec in selected
    )


def test_chinese_objective_uses_deterministic_hints() -> None:
    selected = select_mcp_tool_specs(
        _specs(), "查询 Xenon 仓库的 star 数量", limit=2
    )
    assert selected[0].name == "github:search_repositories"
