"""Deterministic normalisation for pytest commands.

The model often wraps a perfectly valid pytest invocation in PowerShell
plumbing (``$env:...``, ``2>&1 | Select-Object``) or combines it with ``cd``.
Running that through a shell makes the exit code and "no tests ran" state
ambiguous.  This module recognises the narrow pytest case and turns it into a
list of argument vectors that can be executed without a shell.

Anything outside that narrow grammar returns ``None`` and keeps the existing
command behaviour unchanged.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

_PYTEST_RE = re.compile(
    r"^(?:(?:python(?:3(?:\.\d+)?)?|py)(?:\.exe)?\s+-m\s+)?pytest\b(.*)$",
    re.I,
)
_CD_RE = re.compile(r"^(?:cd(?:\s+/d)?|set-location|sl)\s+(.+?)\s*$", re.I)
_ENV_RE = re.compile(
    r"^\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(['\"]?)(.*?)\2\s*$"
)
_REDIRECT_RE = re.compile(r"\s+[12]?>\s*\S+.*$")


@dataclass
class PytestPlan:
    """A shell-free pytest execution plan."""

    cwd: str
    invocations: list[list[str]] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def _split_segments(command: str) -> list[str]:
    """Split ``;``/``&&`` outside quotes, preserving Windows paths."""
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        char = command[i]
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            i += 1
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
            i += 1
            continue
        if char == ";":
            parts.append("".join(current))
            current = []
            i += 1
            continue
        if command.startswith("&&", i):
            parts.append("".join(current))
            current = []
            i += 2
            continue
        current.append(char)
        i += 1
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _strip_unquoted_pipeline(raw: str) -> str:
    """Remove shell pipes/redirects without touching quoted pytest args."""
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(raw):
        char = raw[i]
        if quote:
            out.append(char)
            if char == quote:
                quote = ""
            i += 1
            continue
        if char in ("'", '"'):
            quote = char
            out.append(char)
            i += 1
            continue
        if char == "2" and raw.startswith("2>&1", i):
            i += 4
            continue
        if char == "1" and raw.startswith("1>&2", i):
            i += 4
            continue
        if char == "|" or char == ">":
            break
        out.append(char)
        i += 1
    return "".join(out)


def _clean_args(raw: str) -> list[str]:
    raw = _strip_unquoted_pipeline(raw or "")
    raw = _REDIRECT_RE.sub("", raw).strip()
    if not raw:
        return []
    try:
        args = shlex.split(raw, posix=False)
    except ValueError:
        args = raw.split()
    # shlex(posix=False) preserves surrounding quotes.  Pytest receives these
    # as literal characters, so node ids and -k expressions would not match.
    return [_strip_quotes(arg) for arg in args]


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def plan_pytest(command: str, cwd: str | Path) -> PytestPlan | None:
    """Return a shell-free pytest plan, or ``None`` for unsupported commands."""
    if not command or "pytest" not in command.lower():
        return None
    workspace = Path(cwd)
    current_cwd = str(workspace)
    invocations: list[list[str]] = []
    env: dict[str, str] = {}

    for segment in _split_segments(command):
        env_match = _ENV_RE.match(segment)
        if env_match:
            env[env_match.group(1)] = env_match.group(3)
            continue
        cd_match = _CD_RE.match(segment)
        if cd_match:
            path = _strip_quotes(cd_match.group(1))
            candidate = Path(path)
            current_cwd = str(
                candidate if candidate.is_absolute() else workspace / candidate
            )
            continue
        pytest_match = _PYTEST_RE.match(segment)
        if not pytest_match:
            # A non-pytest command in the chain makes this unsafe to rewrite.
            return None
        invocations.append(_clean_args(pytest_match.group(1)))

    if not invocations:
        return None
    return PytestPlan(cwd=current_cwd, invocations=invocations, env=env)
