"""Rich markup helpers for command output that also carries literal brackets.

Command handlers return rich markup (``[dim]…[/dim]``) because the interactive
REPL renders it.  The same strings also carry identifiers in brackets —
``[lsn-3ffd…]``, ``[fact-40-workspace]``, markdown links — and rich treats
*any* ``[word]`` as a tag, silently dropping the ones it cannot resolve.
Measured through the real TS front-end: ``/memory lessons`` said "共 3 条
教训" and then listed every lesson without its id.

The fix belongs at the rendering boundary: escape the brackets rich does not
own, leave its real syntax alone.  Both renderers (interactive REPL and the
protocol host) call ``escape_literal_tags`` so a lesson id reads the same in
the terminal, the TS front-end and the ledger.
"""

from __future__ import annotations

import re

__all__ = ["escape_literal_tags", "is_markup_tag"]

# Not preceded by a backslash: an already-escaped bracket is literal text,
# and a second pass must not turn ``\[lsn-abc]`` into ``\\[lsn-abc]``.
_TAG = re.compile(r"(?<!\\)\[([^\[\]\n]{1,80})\]")


def is_markup_tag(inner: str) -> bool:
    """Whether rich reads ``[inner]`` as a tag rather than literal text.

    Only syntax rich actually owns counts: a style it can parse, one of
    Xenon's semantic theme tokens, a closing tag, a ``link=`` target or an
    ``@`` meta tag.  A bracketed identifier matches none of them.
    """

    body = inner.strip()
    if not body:
        return False
    if body.startswith(("/", "link=", "@")):
        return True
    from rich.errors import StyleSyntaxError
    from rich.style import Style

    # ``error`` / ``warning`` / … are resolved by Xenon's Theme, not by
    # ``Style.parse``; the token catalog is their single source of truth.
    from xenon.repl.theme import DEFAULT_TOKEN_STYLES

    if body in DEFAULT_TOKEN_STYLES:
        return True
    try:
        Style.parse(body)
    except StyleSyntaxError:
        return False
    return True


def escape_literal_tags(markup: str) -> str:
    """Escape bracketed text rich would otherwise swallow.

    Returns markup safe to hand to ``Console.print(..., markup=True)``:
    real tags survive, literal identifiers keep their brackets.
    """

    def keep_or_escape(match: "re.Match[str]") -> str:
        inner = match.group(1)
        return match.group(0) if is_markup_tag(inner) else "\\[" + inner + "]"

    return _TAG.sub(keep_or_escape, markup)
