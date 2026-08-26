"""Offline tests for GitHub URL handling, auth and repository caching."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import httpx
import pytest

import xenon.nodes.tool_node as tool_module
from xenon.engine.context import AgentContext
from xenon.nodes.tool_node import ToolNode
from xenon.utils.github_reference import parse_github_reference


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.headers = {}
        self.reason_phrase = "OK" if status_code < 400 else "Not Found"

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, *, headers):
        self.requests.append((url, dict(headers)))
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def clear_default_branch_cache():
    tool_module._GITHUB_DEFAULT_BRANCH_CACHE.clear()


@pytest.mark.parametrize(
    ("value", "kind", "slug", "ref", "path", "number"),
    [
        ("owner/repo", "repo", "owner/repo", "", "", None),
        (
            "https://github.com/owner/repo.git?tab=readme#top",
            "repo",
            "owner/repo",
            "",
            "",
            None,
        ),
        (
            "https://github.com/owner/repo/blob/main/src/app.py?plain=1",
            "blob",
            "owner/repo",
            "main",
            "src/app.py",
            None,
        ),
        (
            "https://github.com/owner/repo/tree/develop/src",
            "tree",
            "owner/repo",
            "develop",
            "src",
            None,
        ),
        (
            "https://github.com/owner/repo/issues/42",
            "issue",
            "owner/repo",
            "",
            "",
            42,
        ),
        (
            "https://github.com/owner/repo/pull/7/files",
            "pull",
            "owner/repo",
            "",
            "",
            7,
        ),
        (
            "https://raw.githubusercontent.com/owner/repo/main/README.md",
            "blob",
            "owner/repo",
            "main",
            "README.md",
            None,
        ),
        (
            "git@github.com:owner/repo.git",
            "repo",
            "owner/repo",
            "",
            "",
            None,
        ),
    ],
)
def test_parse_github_reference(value, kind, slug, ref, path, number):
    parsed = parse_github_reference(value)
    assert parsed.kind == kind
    assert parsed.slug == slug
    assert parsed.ref == ref
    assert parsed.path == path
    assert parsed.number == number


def test_parser_rejects_non_github_hosts():
    with pytest.raises(ValueError, match="仅支持 GitHub"):
        parse_github_reference("https://example.com/owner/repo")


def test_parser_rejects_extra_segments_in_owner_repo_shorthand():
    with pytest.raises(ValueError, match="只能是 owner/repo"):
        parse_github_reference("owner/repo/extra")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/orgs/zhipuai/repositories",
        "https://github.com/topics/agents",
        "https://github.com/users/example/projects",
    ],
)
def test_parser_rejects_non_repository_github_routes(url):
    with pytest.raises(ValueError, match="站点页面"):
        parse_github_reference(url)


def test_list_files_resolves_real_default_branch_and_uses_token(monkeypatch):
    client = FakeClient(
        [
            FakeResponse({"default_branch": "trunk"}),
            FakeResponse(
                {
                    "tree": [
                        {"type": "blob", "path": "src/app.py"},
                        {"type": "blob", "path": "README.md"},
                    ]
                }
            ),
        ]
    )
    monkeypatch.setattr(tool_module, "_create_http_client", lambda **kwargs: client)
    monkeypatch.setenv("GITHUB_TOKEN", "private-token")

    node = ToolNode("gh", action_type="github_fetch", repo="owner/repo")
    result = node.execute(AgentContext())

    assert result["success"] is True
    assert result["branch"] == "trunk"
    assert client.requests[0][0] == "https://api.github.com/repos/owner/repo"
    assert client.requests[1][0].endswith("/git/trees/trunk?recursive=1")
    assert client.requests[0][1]["Authorization"] == "Bearer private-token"


def test_blob_url_fetches_exact_file_via_contents_api(monkeypatch):
    encoded = base64.b64encode("print('ok')\n".encode()).decode()
    client = FakeClient([FakeResponse({"encoding": "base64", "content": encoded})])
    monkeypatch.setattr(tool_module, "_create_http_client", lambda **kwargs: client)

    node = ToolNode(
        "gh",
        action_type="github_fetch",
        repo="https://github.com/owner/repo/blob/main/src/app.py?plain=1",
    )
    result = node.execute(AgentContext())

    assert result["success"] is True
    assert result["path"] == "src/app.py"
    assert result["content"] == "print('ok')\n"
    assert client.requests[0][0].endswith("/contents/src/app.py?ref=main")


def test_web_fetch_delegates_pasted_github_url(monkeypatch):
    encoded = base64.b64encode(b"# project\n").decode()
    client = FakeClient([FakeResponse({"encoding": "base64", "content": encoded})])
    monkeypatch.setattr(tool_module, "_create_http_client", lambda **kwargs: client)

    node = ToolNode(
        "web",
        action_type="web_fetch",
        url="https://github.com/owner/repo/blob/main/README.md",
    )
    result = node.execute(AgentContext())

    assert result["success"] is True
    assert result["action_type"] == "github_fetch"
    assert result["content"] == "# project\n"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/zhipuai",
        "https://github.com/orgs/zhipuai/repositories",
        "https://github.com/topics/agents",
    ],
)
def test_web_fetch_treats_github_organization_as_public_html(monkeypatch, url):
    response = httpx.Response(
        200,
        text="<html><body><h1>Zhipu AI</h1><p>Repositories</p></body></html>",
        request=httpx.Request("GET", url),
    )
    client = FakeClient([response])
    monkeypatch.setattr(tool_module, "_create_http_client", lambda **kwargs: client)
    monkeypatch.setattr(tool_module, "_ssrf_check_url", lambda _url: (True, ""))

    result = ToolNode(
        "web",
        action_type="web_fetch",
        url=url,
    ).execute(AgentContext())

    assert result["success"] is True
    assert result["action_type"] == "web_fetch"
    assert "Zhipu AI" in result["content"]


def test_github_rate_limit_falls_back_to_public_html(monkeypatch):
    limited = httpx.Response(
        403,
        headers={"x-ratelimit-remaining": "0"},
        request=httpx.Request("GET", "https://api.github.com/repos/owner/repo"),
    )
    public = httpx.Response(
        200,
        text="<html><body><h1>owner/repo</h1><p>Public README</p></body></html>",
        request=httpx.Request("GET", "https://github.com/owner/repo"),
    )
    clients = iter((FakeClient([limited]), FakeClient([public])))
    monkeypatch.setattr(
        tool_module,
        "_create_http_client",
        lambda **kwargs: next(clients),
    )

    result = ToolNode(
        "gh",
        action_type="github_fetch",
        repo="owner/repo",
        github_action="fetch_readme",
    ).execute(AgentContext())

    assert result["success"] is True
    assert result["degraded"] is True
    assert result["retryable"] is False
    assert "API 限流" in result["content"]
    assert "Public README" in result["content"]


def test_repo_activity_returns_comparable_maintenance_signals(monkeypatch):
    client = FakeClient(
        [
            FakeResponse(
                {
                    "default_branch": "main",
                    "pushed_at": "2026-07-20T00:00:00Z",
                    "updated_at": "2026-07-21T00:00:00Z",
                    "open_issues_count": 12,
                }
            ),
            FakeResponse(
                [
                    {
                        "state": "closed",
                        "created_at": "2026-07-01T00:00:00Z",
                        "updated_at": "2026-07-03T00:00:00Z",
                        "merged_at": "2026-07-03T00:00:00Z",
                    },
                    {
                        "state": "open",
                        "created_at": "2026-07-10T00:00:00Z",
                        "updated_at": "2026-07-21T00:00:00Z",
                        "merged_at": None,
                    },
                ]
            ),
        ]
    )
    monkeypatch.setattr(tool_module, "_create_http_client", lambda **kwargs: client)

    result = ToolNode(
        "gh",
        action_type="github_fetch",
        repo="owner/repo",
        github_action="repo_activity",
    ).execute(AgentContext())

    assert result["success"] is True
    assert result["sample_size"] == 2
    assert result["open_pull_count"] == 1
    assert result["merged_pull_count"] == 1
    assert result["median_merge_hours"] == 48.0
    assert "不能等同于官方 SLA" in result["content"]


def test_tree_url_filters_to_requested_directory(monkeypatch):
    client = FakeClient(
        [
            FakeResponse(
                {
                    "tree": [
                        {"type": "blob", "path": "src/app.py"},
                        {"type": "blob", "path": "tests/test_app.py"},
                    ]
                }
            )
        ]
    )
    monkeypatch.setattr(tool_module, "_create_http_client", lambda **kwargs: client)

    node = ToolNode(
        "gh",
        action_type="github_fetch",
        repo="https://github.com/owner/repo/tree/develop/src",
    )
    result = node.execute(AgentContext())

    assert result["files"] == ["src/app.py"]
    assert result["branch"] == "develop"


@pytest.mark.parametrize(
    ("url", "endpoint", "action"),
    [
        ("https://github.com/owner/repo/issues/12", "/issues/12", "fetch_issue"),
        ("https://github.com/owner/repo/pull/9", "/pulls/9", "fetch_pull"),
    ],
)
def test_issue_and_pull_urls_fetch_discussion(monkeypatch, url, endpoint, action):
    client = FakeClient(
        [
            FakeResponse(
                {
                    "title": "Fix the bug",
                    "state": "open",
                    "user": {"login": "alice"},
                    "body": "Details",
                }
            )
        ]
    )
    monkeypatch.setattr(tool_module, "_create_http_client", lambda **kwargs: client)

    result = ToolNode("gh", action_type="github_fetch", repo=url).execute(
        AgentContext()
    )

    assert result["success"] is True
    assert result["action"] == action
    assert client.requests[0][0].endswith(endpoint)
    assert "Fix the bug" in result["content"]


def test_clone_detects_default_branch_and_keeps_token_out_of_url(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GH_TOKEN", "clone-token")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1] == "ls-remote":
            return SimpleNamespace(
                returncode=0,
                stdout="ref: refs/heads/trunk\tHEAD\n",
                stderr="",
            )
        target = command[-1]
        repo_dir = tool_module.Path(target)
        (repo_dir / ".git").mkdir(parents=True)
        (repo_dir / "README.md").write_text("hello", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tool_module.subprocess, "run", fake_run)
    node = ToolNode(
        "clone",
        action_type="clone_repo",
        repo="https://github.com/owner/repo?tab=readme",
    )
    result = node.execute(AgentContext())

    assert result["success"] is True
    assert result["branch"] == "trunk"
    clone_command, clone_kwargs = calls[1]
    assert clone_command[clone_command.index("-b") + 1] == "trunk"
    assert "clone-token" not in " ".join(clone_command)
    assert (
        clone_kwargs["env"]["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer clone-token"
    )


def test_cached_clone_fetches_and_fast_forwards(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo_dir = tmp_path / ".xenon" / "repos" / "owner_repo"
    (repo_dir / ".git").mkdir(parents=True)
    (repo_dir / "README.md").write_text("cached", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tool_module.subprocess, "run", fake_run)
    node = ToolNode(
        "clone",
        action_type="clone_repo",
        repo="owner/repo",
        branch="main",
    )
    result = node.execute(AgentContext())

    assert result["success"] is True
    assert result["cache_updated"] is True
    assert calls[0][3:6] == ["fetch", "--depth", "1"]
    assert "--ff-only" in calls[1]
