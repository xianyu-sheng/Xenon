"""P0-2 回归：GitHub token 解析要接上已登录的 `gh` CLI。

真实事故（2026-10-17）：机器上 `gh auth login` 已完成，但 Xenon 的
`create_pull_request` 只认 GITHUB_TOKEN / GH_TOKEN / credentials.yaml，
于是 401；模型只能退回 `gh pr create`。
"""

from __future__ import annotations

import types

import pytest

from xenon.utils import github_auth


@pytest.fixture(autouse=True)
def _clear_gh_cache():
    github_auth.clear_github_token_cache()
    yield
    github_auth.clear_github_token_cache()


def _fake_gh(monkeypatch, *, returncode: int = 0, stdout: str = "ghp_from_gh\n"):
    monkeypatch.setattr(github_auth.shutil, "which", lambda name: "/usr/bin/gh")
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(github_auth.subprocess, "run", _run)
    return calls


def test_env_token_wins(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_env")
    calls = _fake_gh(monkeypatch)
    creds = tmp_path / "credentials.yaml"
    creds.write_text("github:\n  token: ghp_creds\n", encoding="utf-8")

    assert github_auth.load_github_token(credentials_path=creds) == "ghp_env"
    assert calls == []


def test_credentials_token_wins_over_gh(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    calls = _fake_gh(monkeypatch)
    creds = tmp_path / "credentials.yaml"
    creds.write_text("github:\n  token: ghp_creds\n", encoding="utf-8")

    assert github_auth.load_github_token(credentials_path=creds) == "ghp_creds"
    assert calls == []


def test_gh_fallback_when_nothing_configured(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    calls = _fake_gh(monkeypatch)
    creds = tmp_path / "credentials.yaml"
    creds.write_text("{}\n", encoding="utf-8")

    assert github_auth.load_github_token(credentials_path=creds) == "ghp_from_gh"
    assert calls == [["/usr/bin/gh", "auth", "token"]]


def test_gh_failure_degrades_to_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    _fake_gh(monkeypatch, returncode=1, stdout="")
    creds = tmp_path / "credentials.yaml"
    creds.write_text("{}\n", encoding="utf-8")

    assert github_auth.load_github_token(credentials_path=creds) == ""


def test_missing_gh_binary_degrades_to_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(github_auth.shutil, "which", lambda name: None)
    creds = tmp_path / "credentials.yaml"
    creds.write_text("{}\n", encoding="utf-8")

    assert github_auth.load_github_token(credentials_path=creds) == ""


def test_gh_result_is_cached(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    calls = _fake_gh(monkeypatch)
    creds = tmp_path / "credentials.yaml"
    creds.write_text("{}\n", encoding="utf-8")

    assert github_auth.load_github_token(credentials_path=creds) == "ghp_from_gh"
    assert github_auth.load_github_token(credentials_path=creds) == "ghp_from_gh"
    assert len(calls) == 1
