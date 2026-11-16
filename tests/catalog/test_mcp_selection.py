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


def test_shared_chinese_words_rank_above_incidental_english_matches() -> None:
    specs = mcp_tool_specs(
        {
            "github:ui_get": (
                "github",
                {
                    "name": "ui_get",
                    "description": "Read a UI resource served by an MCP server.",
                    "inputSchema": {"type": "object"},
                },
            ),
            "trains:query_trains": (
                "trains",
                {
                    "name": "query_trains",
                    "description": "查询两个城市之间的高铁/火车车次、发车时间与票价。",
                    "inputSchema": {"type": "object"},
                },
            ),
        }
    )
    selected = select_mcp_tool_specs(
        specs, "用已配置的 MCP 工具查一下昆山到上海的高铁", limit=2
    )
    assert selected[0].name == "trains:query_trains"


def test_relevant_only_does_not_fill_with_unrelated_tools() -> None:
    """The session contract grows on evidence, not on the budget."""

    selected = select_mcp_tool_specs(
        _specs(), "search repositories", limit=4, relevant_only=True
    )

    assert [spec.name for spec in selected] == ["github:search_repositories"]


def test_relevant_only_leaves_the_contract_unchanged_when_nothing_is_named() -> None:
    """No evidence means no append: the next tool block stays byte-identical."""

    selected = select_mcp_tool_specs(
        _specs(), "completely unrelated words", limit=3, relevant_only=True
    )

    assert selected == ()


def test_relevant_only_ignores_a_single_incidental_description_token() -> None:
    """One shared description word is coincidence, not a reason to append."""

    selected = select_mcp_tool_specs(
        _specs(), "weather", limit=4, relevant_only=True
    )

    assert selected == ()


def test_the_limit_is_a_budget_even_when_only_one_tool_matches() -> None:
    """A single weak match must not shrink the prompt to one option."""

    specs = mcp_tool_specs(
        {
            "github:ui_get": (
                "github",
                {
                    "name": "ui_get",
                    "description": "Read a UI resource served by an MCP server.",
                    "inputSchema": {"type": "object"},
                },
            ),
            "trains:query_trains": (
                "trains",
                {
                    "name": "query_trains",
                    "description": "查询城市之间的车次。",
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
    selected = select_mcp_tool_specs(specs, "随便看看 mcp 能做什么", limit=3)
    assert len(selected) == 3
    assert [spec.name for spec in selected] == sorted(
        spec.name for spec in selected
    )
