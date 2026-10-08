"""Sandbox seam (minimal): read-only mode, command allowlist, backend detection.

Real isolation is delegated to external runtimes (WSL / Docker): Xenon
detects them and reports honestly, but never pretends its in-process checks
are a security boundary.  The software boundary here is fail-safe by design:
read-only mode and command allowlists are deterministic denials in the
executor.
"""

from __future__ import annotations

import os
import shutil


def detect_sandbox_backends() -> list[str]:
    """Available isolation backends (detection only, no probing)."""

    found: list[str] = []
    if shutil.which("wsl.exe") or shutil.which("wsl"):
        found.append("wsl")
    if shutil.which("docker"):
        found.append("docker")
    return found or ["none"]


def read_only_enabled() -> bool:
    return os.environ.get("XENON_READ_ONLY") == "1"


def command_allowlist() -> frozenset[str] | None:
    raw = os.environ.get("XENON_COMMAND_ALLOWLIST", "").strip()
    if not raw:
        return None
    return frozenset(token.strip().lower() for token in raw.split(",") if token.strip())


def command_denial_reason(command_text: str) -> str | None:
    """Reason when the command violates the allowlist, else None."""

    allow = command_allowlist()
    if allow is None:
        return None
    first_token = (command_text or "").strip().split(maxsplit=1)
    if not first_token:
        return None
    token = first_token[0].lower()
    # 常见 shell 包装（python/py/pytest 后面跟参数）按首词匹配。
    if token in allow:
        return None
    return f"命令白名单不允许: {token}（允许: {', '.join(sorted(allow))}）"
