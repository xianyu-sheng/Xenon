"""Narrow, shell-free normalisation for chained git commands.

Only a command made entirely of ``cd`` and safe local ``git`` segments is
rewritten.  Anything containing another program, pipe, substitution or an
unsupported git subcommand returns ``None`` and keeps the existing shell path.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

_CD_RE = re.compile(r"^(?:cd(?:\s+/d)?|set-location|sl)\s+(.+?)\s*$", re.I)
_SAFE_GIT = frozenset(
    {
        "status",
        "diff",
        "add",
        "commit",
        "log",
        "show",
        "rev-parse",
        "checkout",
        "restore",
        "clean",
        "rm",
        "mv",
        "reset",
    }
)


@dataclass
class GitPlan:
    cwd: str
    invocations: list[list[str]] = field(default_factory=list)


def _split_segments(command: str) -> list[str]:
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


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def plan_git(command: str, cwd: str | Path) -> GitPlan | None:
    if not command or "git" not in command.lower():
        return None
    if any(token in command for token in ("$(", "`", "|", ">", "<", "${")):
        return None
    workspace = Path(cwd)
    current_cwd = str(workspace)
    invocations: list[list[str]] = []
    for segment in _split_segments(command):
        cd_match = _CD_RE.match(segment)
        if cd_match:
            raw_path = _strip_quotes(cd_match.group(1))
            candidate = Path(raw_path)
            current_cwd = str(
                candidate if candidate.is_absolute() else workspace / candidate
            )
            continue
        lowered = segment.lower()
        if (
            invocations
            and lowered.startswith(("commit ", "add "))
            and not lowered.startswith("git ")
        ):
            # Models commonly write ``git add x && commit -m ...``; the
            # intent is still a git command, not a shell builtin.
            segment = "git " + segment
        if not segment.lower().startswith("git "):
            return None
        try:
            args = shlex.split(segment, posix=False)
        except ValueError:
            return None
        args = [_strip_quotes(arg) for arg in args]
        if len(args) < 2 or args[0].lower() != "git":
            return None
        if any("$" in arg for arg in args):
            return None
        subcommand = args[1].lower()
        if subcommand not in _SAFE_GIT:
            return None
        invocations.append(args)
    if not invocations:
        return None
    return GitPlan(cwd=current_cwd, invocations=invocations)
