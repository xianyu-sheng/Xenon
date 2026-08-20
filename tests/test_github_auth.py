"""Tests for Xenon's shared GitHub credential resolution."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

import xenon.nodes.tool_node as tool_module
import xenon.repl.command_groups.skill as skill_commands
from xenon.engine.context import AgentContext
from xenon.nodes.tool_node import ToolNode
from xenon.utils.github_auth import github_auth_headers, load_github_token


@pytest.fixture(autouse=True)
def clear_github_auth_environment(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("XENON_CREDENTIALS_PATH", raising=False)


def test_github_token_environment_precedence(tmp_path, monkeypatch):
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text("github:\n  token: file-token\n", encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", " gh-token ")
    monkeypatch.setenv("GITHUB_TOKEN", " github-token ")

    assert load_github_token(credentials_path=credentials) == "github-token"


def test_github_token_falls_back_to_gh_token(tmp_path, monkeypatch):
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text("github:\n  token: file-token\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "  ")
    monkeypatch.setenv("GH_TOKEN", " gh-token ")

    assert load_github_token(credentials_path=credentials) == "gh-token"


def test_github_token_falls_back_to_credentials_file(tmp_path):
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text("github:\n  token: ' saved-token '\n", encoding="utf-8")

    assert load_github_token(credentials_path=credentials) == "saved-token"


def test_github_credentials_path_override(tmp_path, monkeypatch):
    credentials = tmp_path / "isolated.yaml"
    credentials.write_text("github:\n  token: override-token\n", encoding="utf-8")
    monkeypatch.setenv("XENON_CREDENTIALS_PATH", str(credentials))

    assert load_github_token() == "override-token"


@pytest.mark.parametrize(
    "content",
    [
        "",
        "[not, a, mapping]\n",
        "github: []\n",
        "github:\n  token: 123\n",
        "github:\n  token: '   '\n",
        "github: [unterminated\n",
        "github:\n  token: |\n    first\n    second\n",
    ],
)
def test_invalid_github_credentials_are_ignored(tmp_path, content):
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text(content, encoding="utf-8")

    assert load_github_token(credentials_path=credentials) == ""


def test_github_headers_and_clone_auth_use_saved_token(tmp_path, monkeypatch):
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text("github:\n  token: saved-token\n", encoding="utf-8")
    monkeypatch.setenv("XENON_CREDENTIALS_PATH", str(credentials))
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)

    assert ToolNode._github_headers()["Authorization"] == "Bearer saved-token"
    git_env = ToolNode._git_auth_env()
    assert git_env["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer saved-token"


def test_web_fetch_uses_saved_token_for_github_api(tmp_path, monkeypatch):
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text("github:\n  token: saved-token\n", encoding="utf-8")
    monkeypatch.setenv("XENON_CREDENTIALS_PATH", str(credentials))
    response = httpx.Response(
        200,
        text='{"ok": true}',
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "https://api.github.com/rate_limit"),
    )
    requests = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, *, headers):
            requests.append((url, dict(headers)))
            return response

    monkeypatch.setattr(tool_module, "_create_http_client", lambda **kwargs: Client())
    monkeypatch.setattr(tool_module, "_ssrf_check_url", lambda _url: (True, ""))

    result = ToolNode(
        "web",
        action_type="web_fetch",
        url="https://api.github.com/rate_limit",
    ).execute(AgentContext())

    assert result["success"] is True
    assert requests[0][1]["Authorization"] == "Bearer saved-token"
    assert "saved-token" not in str(result)


def test_skill_import_curl_keeps_saved_token_out_of_arguments(tmp_path, monkeypatch):
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text("github:\n  token: saved-token\n", encoding="utf-8")
    monkeypatch.setenv("XENON_CREDENTIALS_PATH", str(credentials))
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(skill_commands.subprocess, "run", fake_run)

    skill_commands._github_curl_get("https://api.github.com/repos/owner/repo/contents")

    assert "saved-token" not in " ".join(captured["command"])
    assert captured["command"][-2:] == ["@-", "https://api.github.com/repos/owner/repo/contents"]
    assert captured["kwargs"]["input"] == "Authorization: Bearer saved-token\n"


def test_auth_helper_does_not_report_token_on_invalid_config(
    tmp_path, caplog, monkeypatch,
):
    credentials = tmp_path / "credentials.yaml"
    secret = "should-never-be-reported"
    credentials.write_text(f"github: [\n  {secret}\n", encoding="utf-8")
    monkeypatch.setenv("XENON_CREDENTIALS_PATH", str(credentials))

    assert load_github_token(credentials_path=credentials) == ""
    assert secret not in caplog.text
    assert github_auth_headers() == {}
