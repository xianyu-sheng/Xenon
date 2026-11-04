"""Turn a failed receipt into a next step the model can act on (#417).

A tool failure used to reach the model as an error string and nothing else: the
agent saw ``PermissionError``/``需要 file_path`` and either repeated the same
call or gave up.  That is a *usability* gap, not a verification one — Xenon
still records the failure either way — so the fix is to say what to do next,
never to block anything.

Design rules, in the same spirit as the rest of the harness:

* **advice only**: this module never executes, never blocks, never mutates a
  receipt.  It appends one note to the next view;
* **derived, not guessed**: every branch keys off the receipt's own error text
  or the tool's declared schema, so the same failure always gets the same
  advice (replay-safe, no clock, no I/O);
* **bounded**: at most one advice line per failure, and the note list the view
  shows is already capped.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

__all__ = ["advice_for_failure"]

#: (pattern, advice).  Ordered: the first match wins, so put the specific
#: cases before the generic ones.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"需要\s*\w+|is required|missing required|required argument", re.I),
        "The call is missing a required parameter.  Read the tool's `params` in "
        "this view and pass exactly those names; if the task is already done, "
        "submit `finish` instead of retrying.",
    ),
    (
        re.compile(r"not found|不存在|no such file|ENOENT", re.I),
        "That path does not exist.  Search the workspace first (it also tells "
        "you the real name), then read the file it reports.",
    ),
    (
        re.compile(r"is a directory|这是一个目录|EISDIR", re.I),
        "That path is a directory.  Use `search` to list it, or read a file "
        "inside it.",
    ),
    (
        re.compile(r"permission denied|拒绝访问|EACCES|Errno 13", re.I),
        "The operating system refused this path.  It is not an approval issue: "
        "the path itself is not readable/writable by this process.  Pick another "
        "location in the workspace.",
    ),
    (
        re.compile(r"path (?:escapes|is outside)|路径越界|outside the workspace", re.I),
        "The target is outside the workspace fence.  Either work inside the "
        "workspace, or ask the operator to allow that directory — the refusal "
        "is a decision, not a bug.",
    ),
    (
        re.compile(r"timed? ?out|timeout|超时", re.I),
        "The call timed out.  Do not repeat it unchanged: narrow the target "
        "(one file, a smaller range) or use a cheaper check.",
    ),
    (
        re.compile(r"ambiguous|多匹配|multiple matches", re.I),
        "Several resources match.  Name exactly one of the candidates reported "
        "by the error before calling again.",
    ),
    (
        re.compile(r"exit code|non-zero exit|status 4\d\d|status 5\d\d", re.I),
        "The command exited non-zero.  Read its stderr above; the next call "
        "should address that specific error, not re-run the command.",
    ),
)

_GENERIC = (
    "That call failed.  The error above is the evidence: change one thing "
    "(target, parameter, or tool) before calling again."
)


def _error_text(receipt: Mapping[str, Any]) -> str:
    parts = [str(receipt.get("error") or "")]
    for effect in receipt.get("effects") or ():
        if isinstance(effect, Mapping):
            parts.append(str(effect.get("error") or ""))
    return " ".join(part for part in parts if part)


def advice_for_failure(receipt: Mapping[str, Any]) -> str:
    """One actionable sentence for a failed receipt, or ``""``.

    ``receipt`` is the ledger payload's ``receipt`` mapping (the same shape the
    view already reads), so this works on a replayed ledger with no live objects.
    """

    status = str(receipt.get("status") or "")
    if status not in {"failed", "denied", "timeout", "timed_out"}:
        return ""
    text = _error_text(receipt)
    for pattern, advice in _RULES:
        if pattern.search(text):
            return advice
    return _GENERIC
