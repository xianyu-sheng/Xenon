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


#: Git subcommands that only read.  ``branch`` is here because listing is the
#: common case; creating or deleting a branch is caught by the flag check or by
#: the other write subcommands simply not being in this set.
READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {"status", "diff", "log", "show", "branch", "rev-parse", "blame"}
)
_BRANCH_WRITE_FLAGS = ("-d", "-D", "-m", "-M", "-c", "--delete", "--move")

#: Anything that turns one command into more than a git invocation.  A chain of
#: ``&&``/``;`` segments is handled by ``_split_segments`` below; every other
#: metacharacter means "not a bare git query".
_SHELL_META = ("|", "<", ">", "`", "$(", "\n")


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


def _read_only_segment(segment: str) -> bool:
    if any(marker in segment for marker in _SHELL_META):
        return False
    try:
        tokens = [
            item.strip("\"'").casefold()
            for item in shlex.split(segment, posix=False)
            if item.strip()
        ]
    except ValueError:
        return False
    if tokens[:1] == ["git"]:
        tokens = tokens[1:]
    if not tokens:
        return False
    subjects = [item for item in tokens if not item.startswith("-")]
    if not subjects:
        return False
    subject = subjects[0]
    if subject == "branch":
        return not any(flag in tokens for flag in _BRANCH_WRITE_FLAGS)
    return subject in READ_ONLY_GIT_SUBCOMMANDS


def is_read_only_git(command: str) -> bool:
    """True when every segment of ``command`` is a bare read-only git query.

    This is the one predicate both the kernel permission policy and the
    harness action parser consult, so "the model may propose a git query" and
    "the policy will allow it" cannot drift apart.  Chains split on ``;`` and
    ``&&`` are accepted only when each segment is itself read-only, so
    ``git status && git log`` reads and ``git status && rm -rf build`` does
    not.  Pipes, redirects, substitutions and newlines are never read-only.
    """

    text = (command or "").strip()
    if not text:
        return False
    segments = _split_segments(text)
    if not segments:
        return False
    return all(_read_only_segment(segment) for segment in segments)


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
