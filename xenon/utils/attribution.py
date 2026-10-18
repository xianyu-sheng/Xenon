"""Forced Xenon attribution for commits and pull requests.

Every commit made through Xenon carries a ``Co-authored-by`` trailer and every
PR created through the ``create_pull_request`` tool carries a footer, so it is
visible which tool produced the change.  Attribution is on by default; set
``XENON_ATTRIBUTION=0`` to turn it off.

The trailer is added with ``git commit --trailer`` (Git >= 2.32), which works
for both ``-m`` and editor commits and cannot be skipped by ``--no-verify``.
"""

from __future__ import annotations

import os
import re

TRAILER = "Co-authored-by: Xenon <noreply@xenon.dev>"
REPOSITORY_URL = "https://github.com/xianyu-sheng/Xenon"

_DISABLED_VALUES = frozenset({"0", "false", "off", "no", "disable", "disabled"})


def attribution_enabled() -> bool:
    """Attribution is on unless ``XENON_ATTRIBUTION`` explicitly disables it."""

    raw = os.environ.get("XENON_ATTRIBUTION", "").strip().lower()
    return raw not in _DISABLED_VALUES


def pr_footer() -> str:
    """Markdown footer appended to pull-request bodies created by Xenon."""

    from xenon import __version__

    return (
        "\n\n---\n"
        f"🤖 Generated with [Xenon]({REPOSITORY_URL}) v{__version__}"
    )


def _has_attribution(text: str) -> bool:
    lowered = (text or "").lower()
    return "co-authored-by: xenon" in lowered or "generated with [xenon]" in lowered


# ``git commit`` with an optional ``-C <path>``; the trailer is inserted right
# after ``commit`` so pipes/redirections and quoted messages stay untouched.
_GIT_COMMIT_RE = re.compile(
    r"\bgit(?:\s+-C\s+(?:\"[^\"]*\"|'[^']*'|\S+))?\s+commit\b",
    re.IGNORECASE,
)


def ensure_commit_attribution(command: str) -> str:
    """Insert the Xenon trailer into every ``git commit`` in a shell command.

    Handles common forms such as ``git commit -m "x"``, ``git -C repo commit``
    and chained commands.  Returns the command unchanged when attribution is
    disabled, no commit is present, or a Xenon trailer already exists.
    """

    if not command or not attribution_enabled() or _has_attribution(command):
        return command
    if not _GIT_COMMIT_RE.search(command):
        return command

    quoted = f' --trailer "{TRAILER}"'

    def _insert(match: re.Match[str]) -> str:
        return match.group(0) + quoted

    return _GIT_COMMIT_RE.sub(_insert, command)
