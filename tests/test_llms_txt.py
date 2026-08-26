"""llms.txt parsing, ranking, and docs_fetch integration tests."""

from __future__ import annotations

import httpx

from xenon.engine.context import AgentContext
from xenon.engine.react_engine import BUILTIN_TOOLS
from xenon.nodes.tool_executor import ToolExecutor, classify_tool
from xenon.nodes.tool_node import ToolNode
from xenon.utils.llms_txt import (
    llms_candidate_urls,
    parse_llms_txt,
    select_llms_links,
)


INDEX = """\ufeff# Example SDK

> Official SDK documentation for agents.

Use the API reference for exact request fields.

## Guides

- [Quickstart](/docs/quickstart.md): Install and make the first request
- [Function calling](/docs/tools.md): Define and invoke agent tools

## Optional

- [Migration](https://archive.example/migrate.md): Legacy migration guide
"""


class _Response:
    def __init__(
        self,
        url: str,
        text: str = "",
        status: int = 200,
        content_type: str = "text/markdown",
    ) -> None:
        self.url = httpx.URL(url)
        self.text = text
        self.status_code = status
        self.headers = {"content-type": content_type}
        self.is_redirect = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", self.url)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=response
            )


class _Client:
    def __init__(self, responses: dict[str, _Response]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url, headers=None):
        target = str(url)
        self.calls.append(target)
        return self.responses.get(target, _Response(target, status=404))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _patch_http(monkeypatch, responses: dict[str, _Response]) -> _Client:
    client = _Client(responses)
    monkeypatch.setattr(
        "xenon.nodes.tool_node._create_http_client",
        lambda **kwargs: client,
    )
    monkeypatch.setattr(
        "xenon.nodes.tool_node._ssrf_check_url",
        lambda url: (True, ""),
    )
    return client


def test_parse_spec_sections_relative_links_and_optional() -> None:
    document = parse_llms_txt(INDEX, "https://docs.example/llms.txt")

    assert document.title == "Example SDK"
    assert document.summary == "Official SDK documentation for agents."
    assert "exact request fields" in document.details
    assert [link.url for link in document.links] == [
        "https://docs.example/docs/quickstart.md",
        "https://docs.example/docs/tools.md",
        "https://archive.example/migrate.md",
    ]
    assert document.links[-1].optional is True


def test_parse_requires_h1() -> None:
    try:
        parse_llms_txt("## Docs\n- [A](/a.md)", "https://docs.example/llms.txt")
    except ValueError as exc:
        assert "H1" in str(exc)
    else:
        raise AssertionError("missing H1 must be rejected")


def test_query_ranking_uses_title_description_and_skips_unmatched() -> None:
    document = parse_llms_txt(INDEX, "https://docs.example/llms.txt")
    selected = select_llms_links(document, "agent function tools", max_pages=2)

    assert [link.title for link in selected] == ["Function calling"]


def test_no_query_prefers_core_before_optional() -> None:
    document = parse_llms_txt(INDEX, "https://docs.example/llms.txt")
    selected = select_llms_links(document, max_pages=3)
    assert [link.title for link in selected] == [
        "Quickstart",
        "Function calling",
        "Migration",
    ]


def test_candidate_discovery_is_bounded_and_subpath_first() -> None:
    assert llms_candidate_urls("https://docs.example/docs/api/page.html?x=1") == [
        "https://docs.example/docs/llms.txt",
        "https://docs.example/llms.txt",
        "https://docs.example/docs/llms-full.txt",
        "https://docs.example/llms-full.txt",
    ]
    assert llms_candidate_urls("https://docs.example/llms-full.txt?token=x") == [
        "https://docs.example/llms-full.txt"
    ]


def test_docs_fetch_selects_query_relevant_page(monkeypatch) -> None:
    client = _patch_http(
        monkeypatch,
        {
            "https://docs.example/docs/llms.txt": _Response(
                "https://docs.example/docs/llms.txt", INDEX
            ),
            "https://docs.example/docs/tools.md": _Response(
                "https://docs.example/docs/tools.md",
                "# Tools\nUse tool_choice and JSON Schema.",
            ),
        },
    )
    node = ToolNode(
        "docs",
        action_type="docs_fetch",
        url="https://docs.example/docs/api/reference",
        query="function tools",
        max_pages=2,
    )

    result = node.execute(AgentContext())

    assert result["success"] is True
    assert result["strategy"] == "llms-index"
    assert result["discovered_links"] == 3
    assert result["optional_links"] == 1
    assert result["selected_sources"] == ["https://docs.example/docs/tools.md"]
    assert "Use tool_choice" in result["content"]
    assert "Quickstart" not in result["content"]
    assert client.calls == [
        "https://docs.example/docs/llms.txt",
        "https://docs.example/docs/tools.md",
    ]


def test_docs_fetch_converts_linked_html_to_text(monkeypatch) -> None:
    _patch_http(
        monkeypatch,
        {
            "https://docs.example/llms.txt": _Response(
                "https://docs.example/llms.txt", INDEX
            ),
            "https://docs.example/docs/quickstart.md": _Response(
                "https://docs.example/docs/quickstart.md",
                "<html><body><h1>Install</h1><script>bad()</script><p>pip install sdk</p></body></html>",
                content_type="text/html",
            ),
        },
    )

    result = ToolNode(
        "docs",
        action_type="docs_fetch",
        url="https://docs.example/",
        query="install quickstart",
        max_pages=1,
    ).execute(AgentContext())

    assert "pip install sdk" in result["content"]
    assert "bad()" not in result["content"]


def test_docs_fetch_uses_full_file_when_index_missing(monkeypatch) -> None:
    client = _patch_http(
        monkeypatch,
        {
            "https://docs.example/llms-full.txt": _Response(
                "https://docs.example/llms-full.txt", "# Full docs\n" + "x" * 4000
            ),
        },
    )

    result = ToolNode(
        "docs",
        action_type="docs_fetch",
        url="https://docs.example/guide",
        max_chars=1000,
    ).execute(AgentContext())

    assert result["strategy"] == "llms-full"
    assert result["truncated"] is True
    assert len(result["content"]) <= 1000
    assert client.calls[-1] == "https://docs.example/llms-full.txt"


def test_docs_fetch_falls_back_to_requested_html(monkeypatch) -> None:
    _patch_http(
        monkeypatch,
        {
            "https://docs.example/guide": _Response(
                "https://docs.example/guide",
                "<html><body><h1>Guide</h1><p>Fallback works.</p></body></html>",
                content_type="text/html",
            ),
        },
    )

    result = ToolNode(
        "docs",
        action_type="docs_fetch",
        url="https://docs.example/guide",
    ).execute(AgentContext())

    assert result["success"] is True
    assert result["strategy"] == "html-fallback"
    assert result["degraded"] is True
    assert "Fallback works" in result["content"]
    assert len(result["discovery_attempts"]) == 4


def test_link_failure_is_isolated_from_other_selected_pages(monkeypatch) -> None:
    _patch_http(
        monkeypatch,
        {
            "https://docs.example/llms.txt": _Response(
                "https://docs.example/llms.txt", INDEX
            ),
            "https://docs.example/docs/quickstart.md": _Response(
                "https://docs.example/docs/quickstart.md", status=503
            ),
            "https://docs.example/docs/tools.md": _Response(
                "https://docs.example/docs/tools.md", "# Tools\nworking"
            ),
        },
    )

    result = ToolNode(
        "docs",
        action_type="docs_fetch",
        url="https://docs.example/",
        max_pages=2,
    ).execute(AgentContext())

    assert result["success"] is True
    assert result["selected_sources"] == ["https://docs.example/docs/tools.md"]
    assert result["source_errors"][0]["url"].endswith("quickstart.md")


def test_private_link_from_index_is_blocked_before_request(monkeypatch) -> None:
    index = """# Unsafe index

## Docs

- [Metadata](http://169.254.169.254/latest): Never fetch this
"""
    client = _Client(
        {
            "https://docs.example/llms.txt": _Response(
                "https://docs.example/llms.txt", index
            ),
        }
    )
    monkeypatch.setattr(
        "xenon.nodes.tool_node._create_http_client", lambda **kwargs: client
    )
    monkeypatch.setattr(
        "xenon.nodes.tool_node._ssrf_check_url",
        lambda url: (
            (False, "private metadata address")
            if "169.254.169.254" in url
            else (True, "")
        ),
    )

    result = ToolNode(
        "docs",
        action_type="docs_fetch",
        url="https://docs.example/",
        query="metadata",
        max_pages=1,
    ).execute(AgentContext())

    assert result["success"] is True
    assert result["selected_sources"] == []
    assert "SSRF" in result["source_errors"][0]["error"]
    assert client.calls == ["https://docs.example/llms.txt"]


def test_tool_executor_accepts_query_alias_and_keeps_large_doc_observation(
    monkeypatch,
) -> None:
    body = "# Tools\n" + "z" * 6000
    _patch_http(
        monkeypatch,
        {
            "https://docs.example/llms-full.txt": _Response(
                "https://docs.example/llms-full.txt", body
            ),
        },
    )
    result = ToolExecutor(retry_attempts=1).execute(
        "docs_fetch",
        {"url": "https://docs.example/llms-full.txt", "query": "tools"},
        AgentContext(),
    )

    assert result.success is True
    assert len(result.observation) > 5000
    assert result.raw["strategy"] == "llms-full"


def test_docs_fetch_is_read_only_and_published_to_react_schema() -> None:
    assert classify_tool("docs_fetch") == "INFO"
    assert "docs_fetch" in BUILTIN_TOOLS
    assert set(BUILTIN_TOOLS["docs_fetch"]["params"]) == {
        "url",
        "query",
        "max_pages",
        "max_chars",
    }
