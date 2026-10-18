"""强制署名：git commit 的 Co-authored-by trailer 与 PR 页脚。"""

from __future__ import annotations

from xenon.utils.attribution import (
    TRAILER,
    attribution_enabled,
    ensure_commit_attribution,
    pr_footer,
)


def test_attribution_enabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("XENON_ATTRIBUTION", raising=False)
    assert attribution_enabled() is True


def test_attribution_can_be_disabled(monkeypatch) -> None:
    for raw in ("0", "false", "OFF", "no"):
        monkeypatch.setenv("XENON_ATTRIBUTION", raw)
        assert attribution_enabled() is False


def test_simple_commit_gets_trailer() -> None:
    out = ensure_commit_attribution('git commit -m "feat: x"')
    assert "--trailer" in out
    assert TRAILER in out
    assert out.index("--trailer") < out.index("-m")


def test_chained_command_and_pipe_keep_trailer_position() -> None:
    original = 'cd repo; git commit -m "fix: y" 2>&1 | Select-Object -Last 5'
    out = ensure_commit_attribution(original)
    assert "git commit --trailer" in out
    assert out.endswith("Select-Object -Last 5")
    assert '2>&1' in out


def test_git_dash_c_commit() -> None:
    out = ensure_commit_attribution('git -C "some path" commit -m "x"')
    assert TRAILER in out


def test_non_commit_command_unchanged() -> None:
    original = "git status --short && pytest -q"
    assert ensure_commit_attribution(original) == original


def test_existing_attribution_is_not_duplicated() -> None:
    original = f'git commit -m "x" --trailer "{TRAILER}"'
    assert ensure_commit_attribution(original) == original


def test_disabled_env_leaves_command_unchanged(monkeypatch) -> None:
    monkeypatch.setenv("XENON_ATTRIBUTION", "0")
    original = 'git commit -m "x"'
    assert ensure_commit_attribution(original) == original


def test_pr_footer_mentions_repository_and_version() -> None:
    footer = pr_footer()
    assert "github.com/xianyu-sheng/Xenon" in footer
    assert "Generated with [Xenon]" in footer
