"""D-4 回归：create_pull_request 工具（GitHub API，mock httpx）。"""

from __future__ import annotations

import pytest

from xenon.engine.context import AgentContext
from xenon.nodes.tool_node import ToolNode


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """httpx.Client 的最小替身，记录请求供断言。"""

    last_post: dict | None = None
    last_get: str | None = None
    response: _FakeResponse = _FakeResponse(201, {"number": 7, "html_url": "https://x/pull/7"})
    default_branch: str = "main"

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def get(self, url: str, headers=None, **kwargs) -> _FakeResponse:
        type(self).last_get = url
        return _FakeResponse(200, {"default_branch": type(self).default_branch})

    def post(self, url: str, headers=None, json=None, **kwargs) -> _FakeResponse:
        type(self).last_post = {"url": url, "headers": headers, "json": json}
        return type(self).response


@pytest.fixture
def fake_httpx(monkeypatch):
    import httpx

    _FakeClient.last_post = None
    _FakeClient.last_get = None
    monkeypatch.delenv("XENON_ATTRIBUTION", raising=False)
    _FakeClient.response = _FakeResponse(
        201, {"number": 7, "html_url": "https://github.com/o/r/pull/7"}
    )
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    return _FakeClient


def _node(**params) -> ToolNode:
    return ToolNode("t", action_type="create_pull_request", **params)


def test_create_pr_success(fake_httpx) -> None:
    result = _node(
        repo="o/r", branch="fix/x", pr_title="Fix X", pr_body="body", base_branch="main"
    ).execute(AgentContext({}))

    assert result["success"] is True
    assert result["pr_number"] == 7
    assert result["pr_url"].endswith("/pull/7")
    payload = fake_httpx.last_post["json"]
    assert payload["title"] == "Fix X"
    assert payload["head"] == "fix/x"
    assert payload["base"] == "main"
    assert payload["body"].startswith("body")
    assert "Generated with [Xenon]" in payload["body"]
    assert fake_httpx.last_post["url"].endswith("/repos/o/r/pulls")


def test_create_pr_resolves_default_branch(fake_httpx) -> None:
    _node(repo="o/r", branch="fix/x", pr_title="T").execute(AgentContext({}))

    assert fake_httpx.last_get is not None
    assert fake_httpx.last_post["json"]["base"] == "main"


def test_create_pr_requires_head_and_title(fake_httpx) -> None:
    result = _node(repo="o/r").execute(AgentContext({}))
    assert result["success"] is False
    assert "branch" in result["error"]
    assert fake_httpx.last_post is None


def test_create_pr_surfaces_auth_error(fake_httpx) -> None:
    fake_httpx.response = _FakeResponse(403, {"message": "Bad credentials"})
    result = _node(
        repo="o/r", branch="fix/x", pr_title="T", base_branch="main"
    ).execute(AgentContext({}))

    assert result["success"] is False
    assert result["status_code"] == 403
    assert "权限" in result["error"]


def test_pr_tool_is_registered_and_requires_approval() -> None:
    from xenon.engine.react_prompts import BUILTIN_TOOLS
    from xenon.nodes.tool_executor import classify_tool
    from xenon.nodes.tool_registry import BUILTIN_TOOL_REGISTRY
    from xenon.repl.permissions import _CRITICAL_TOOLS

    assert "create_pull_request" in BUILTIN_TOOLS
    assert "create_pull_request" in BUILTIN_TOOL_REGISTRY.names()
    assert "create_pull_request" in _CRITICAL_TOOLS
    assert classify_tool("create_pull_request", {}) == "SENSITIVE"


def test_pr_footer_is_not_duplicated(fake_httpx) -> None:
    from xenon.utils.attribution import pr_footer

    _node(
        repo="o/r",
        branch="fix/x",
        pr_title="T",
        base_branch="main",
        pr_body="body" + pr_footer(),
    ).execute(AgentContext({}))

    body = fake_httpx.last_post["json"]["body"]
    assert body.lower().count("generated with [xenon]") == 1


def test_pr_footer_can_be_disabled(fake_httpx, monkeypatch) -> None:
    monkeypatch.setenv("XENON_ATTRIBUTION", "0")
    _node(
        repo="o/r", branch="fix/x", pr_title="T", base_branch="main", pr_body="body"
    ).execute(AgentContext({}))

    assert fake_httpx.last_post["json"]["body"] == "body"
