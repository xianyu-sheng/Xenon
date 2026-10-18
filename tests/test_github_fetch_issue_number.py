"""P1-2：github_fetch(fetch_issue/fetch_pull) 支持用 github_path 传编号。"""

from __future__ import annotations

import pytest

import xenon.nodes.tool_node as tool_module
from xenon.engine.context import AgentContext
from xenon.nodes.tool_families.github_tools import _issue_number_hint
from xenon.nodes.tool_node import ToolNode


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("27", 27),
        ("#27", 27),
        ("issues/27", 27),
        ("issue/27", 27),
        ("pulls/27", 27),
        ("pr/27", 27),
        ("  #27  ", 27),
        ("", None),
        ("README.md", None),
        ("27abc", None),
    ],
)
def test_issue_number_hint(raw: str, expected: int | None) -> None:
    assert _issue_number_hint(raw) == expected


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        pass


class _FakeClient:
    requested: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def get(self, url: str, headers=None, **kwargs) -> _FakeResponse:
        type(self).requested.append(url)
        return _FakeResponse(
            {
                "number": 27,
                "title": "Source-unit metadata is zero",
                "state": "open",
                "body": "line_count is always 0",
                "user": {"login": "xianyu-sheng"},
                "created_at": "2026-10-01T00:00:00Z",
                "updated_at": "2026-10-02T00:00:00Z",
            }
        )


@pytest.fixture
def fake_client(monkeypatch):
    _FakeClient.requested = []
    monkeypatch.setattr(
        tool_module, "_create_http_client", lambda *a, **k: _FakeClient()
    )
    return _FakeClient


def test_fetch_issue_uses_github_path_number(fake_client) -> None:
    result = ToolNode(
        "t",
        action_type="github_fetch",
        repo="xianyu-sheng/SmartBench",
        github_action="fetch_issue",
        github_path="27",
    ).execute(AgentContext({}))

    assert result["success"] is True
    assert result["number"] == 27
    assert "Source-unit metadata is zero" in result["content"]
    assert fake_client.requested[-1].endswith("/repos/xianyu-sheng/SmartBench/issues/27")


def test_fetch_issue_accepts_issues_slash_number(fake_client) -> None:
    result = ToolNode(
        "t",
        action_type="github_fetch",
        repo="xianyu-sheng/SmartBench",
        github_action="fetch_issue",
        github_path="issues/27",
    ).execute(AgentContext({}))

    assert result["success"] is True
    assert fake_client.requested[-1].endswith("/issues/27")


def test_missing_number_still_lists_issues(fake_client) -> None:
    result = ToolNode(
        "t",
        action_type="github_fetch",
        repo="xianyu-sheng/SmartBench",
        github_action="fetch_issue",
        github_path="",
    ).execute(AgentContext({}))

    # 无编号时保持既有语义：列出 open issues 概览（而不是报错）。
    assert result["success"] is True
    assert fake_client.requested[-1].endswith("/issues?state=open&per_page=10&sort=created&direction=desc")
