"""Shared GitHub authentication helpers.

GitHub credentials are resolved at call time so tests, sandboxes, and long-lived
Xenon processes can override the credentials file without reloading modules.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def _normalize_token(value: Any) -> str:
    """Return a usable single-line token, or an empty string."""
    if not isinstance(value, str):
        return ""
    token = value.strip()
    if not token or "\n" in token or "\r" in token:
        return ""
    return token


@lru_cache(maxsize=1)
def _gh_auth_token() -> str:
    """Read the token from the logged-in ``gh`` CLI as a last resort.

    The interactive user often has ``gh auth login`` done (keyring) while
    neither ``GITHUB_TOKEN``/``GH_TOKEN`` nor ``credentials.yaml`` is set.
    ``gh auth token`` prints the token to stdout; anything else (missing gh,
    not logged in, timeout) degrades to an empty string so this fallback can
    never break the normal credential path.  The result is cached per process.
    """

    gh = shutil.which("gh")
    if not gh:
        return ""
    try:
        proc = subprocess.run(
            [gh, "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return _normalize_token(proc.stdout)


def clear_github_token_cache() -> None:
    """Drop the cached ``gh auth token`` result (tests, or after re-login)."""

    _gh_auth_token.cache_clear()


def github_credentials_path() -> Path:
    """Resolve Xenon's credentials file, honoring evaluation isolation.

    Goes through the shared config loader so this agrees with
    ``provider_registry._get_credentials_path()``.  Reading the env var
    directly here would let the two diverge as soon as ``paths.credentials``
    is set in ``config.yaml`` but not in the environment.
    """
    from xenon.repl.system_config import get_config

    return Path(get_config().paths.credentials).expanduser()


def load_github_token(*, credentials_path: str | Path | None = None) -> str:
    """Load a GitHub token without exposing it in diagnostics.

    Precedence is ``GITHUB_TOKEN``, then ``GH_TOKEN``, then
    ``credentials.yaml``'s ``github.token`` value, then the logged-in ``gh``
    CLI (``gh auth token``).  The gh fallback is what makes tools usable on a
    machine that already has ``gh`` authenticated but no Xenon token.
    """
    for env_name in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = _normalize_token(os.environ.get(env_name))
        if token:
            return token

    path = (
        Path(credentials_path).expanduser()
        if credentials_path is not None
        else github_credentials_path()
    )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return ""
    if not isinstance(data, dict):
        return ""
    github = data.get("github")
    if isinstance(github, dict):
        token = _normalize_token(github.get("token"))
        if token:
            return token

    return _gh_auth_token()


def github_auth_headers() -> dict[str, str]:
    """Return an Authorization header when a GitHub token is configured."""
    token = load_github_token()
    return {"Authorization": f"Bearer {token}"} if token else {}
