"""Tool-boundary approval policy.

Decisions (user-approved, modelled on Claude Code / Codex / DeepSeek
Harness):

* workspace writes are free;
* out-of-workspace writes and command execution ask;
* "always allow" is a **session rule with a TTL**, not a permanent grant;
* outcomes are closed and fail-closed: only ``allowed-once`` grants.

The pure helpers here decide *whether* a call needs the boundary check and
which paths it touches.  The human channel (prompt, workspace containment,
rule storage) lives at the REPL boundary so library users keep the old
level-based semantics when no approval callback is registered.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

APPROVAL_ALLOWED_ONCE = "allowed-once"
APPROVAL_REJECTED = "rejected"
APPROVAL_CANCELLED = "cancelled"
APPROVAL_UNAVAILABLE = "unavailable"

APPROVAL_OUTCOMES = frozenset(
    {
        APPROVAL_ALLOWED_ONCE,
        APPROVAL_REJECTED,
        APPROVAL_CANCELLED,
        APPROVAL_UNAVAILABLE,
    }
)

DEFAULT_RULE_TTL_SECONDS = 1800.0


def approval_rule_ttl_seconds() -> float:
    """TTL for "always allow" rules; ``XENON_APPROVAL_TTL`` overrides."""

    raw = os.environ.get("XENON_APPROVAL_TTL", "").strip()
    if not raw:
        return DEFAULT_RULE_TTL_SECONDS
    try:
        return max(1.0, float(raw))
    except ValueError:
        return DEFAULT_RULE_TTL_SECONDS


class SessionRuleStore:
    """Session-scoped "always allow" rules with expiry."""

    def __init__(self, ttl_seconds: float | None = None) -> None:
        self.ttl_seconds = (
            approval_rule_ttl_seconds() if ttl_seconds is None else float(ttl_seconds)
        )
        self._expires_at: dict[str, float] = {}

    def allow(self, tool_name: str, *, ttl_seconds: float | None = None) -> float:
        ttl = self.ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        expires = time.monotonic() + max(1.0, ttl)
        self._expires_at[str(tool_name)] = expires
        return ttl

    def is_allowed(self, tool_name: str, *, now: float | None = None) -> bool:
        key = str(tool_name)
        expires = self._expires_at.get(key)
        if expires is None:
            return False
        if (time.monotonic() if now is None else now) >= expires:
            self._expires_at.pop(key, None)
            return False
        return True

    def clear(self) -> None:
        self._expires_at.clear()

    def active_rules(self) -> tuple[str, ...]:
        return tuple(name for name in self._expires_at if self.is_allowed(name))


_WRITE_TARGET_KEYS = ("file_path", "dir_path", "path")


def write_targets_for(tool_name: str, params: dict) -> list[str]:
    """Best-effort extraction of the paths a write tool will touch."""

    if tool_name in {"batch_write", "batch_edit"}:
        items = params.get("files") or params.get("edits") or []
        targets: list[str] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                for key in _WRITE_TARGET_KEYS:
                    if item.get(key):
                        targets.append(str(item[key]))
                        break
        return targets
    for key in _WRITE_TARGET_KEYS:
        if params.get(key):
            return [str(params[key])]
    return []


def needs_boundary_approval(tool_name: str, params: dict) -> bool:
    """Write-level and execute-level tools go through the boundary check.

    Read-only tools never ask.  The actual workspace test happens at the REPL
    boundary, because the low-level executor does not know the workspace.
    """

    from xenon.nodes.tool_executor import required_execution_level

    return required_execution_level(tool_name, params) >= 2


def targets_outside_workspace(
    tool_name: str, params: dict, workspace: Path
) -> bool:
    """True when a write tool touches a path outside *workspace*.

    Unknown/missing targets return True (ask) because the call cannot be
    proven to stay inside the workspace.
    """

    from xenon.nodes.path_targets import resolve_target_path

    raw_targets = write_targets_for(tool_name, params)
    if not raw_targets:
        return True
    root = Path(workspace).resolve()
    for raw in raw_targets:
        try:
            resolved = resolve_target_path(raw, cwd=root)
        except Exception:  # noqa: BLE001 — unparsable target must ask
            return True
        try:
            resolved.resolve(strict=False).relative_to(root)
        except ValueError:
            return True
    return False
