"""Shared GitHub authentication helpers.

GitHub credentials are resolved at call time so tests, sandboxes, and long-lived
Xenon processes can override the credentials file without reloading modules.
"""

from __future__ import annotations

import os
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
    ``credentials.yaml``'s ``github.token`` value.
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
    if not isinstance(github, dict):
        return ""
    return _normalize_token(github.get("token"))


def github_auth_headers() -> dict[str, str]:
    """Return an Authorization header when a GitHub token is configured."""
    token = load_github_token()
    return {"Authorization": f"Bearer {token}"} if token else {}
