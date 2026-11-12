"""One place that decides what a credential looks like.

The report, the MCP registry and the memory guard each carried their own list,
and the three disagreed: a key shape caught by one leaked through another.  A
credential is a credential no matter which subsystem is about to persist it, so
the patterns live here and every writer calls the same function.

Two operations, deliberately different:

* :func:`redact_text` rewrites what is about to be persisted;
* :func:`contains_secret` answers a yes/no question without rewriting.

Neither is a security boundary.  They remove the shapes we can recognise from
text we are about to print or store; they are not a substitute for keeping
credentials out of the pipeline in the first place.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

#: Parameter names whose *values* must never be printed or persisted.  One set
#: for the same reason as the text patterns: the engine's logger and the tool
#: result store each carried their own list, and each was missing keys the
#: other had (``refresh_token``, ``client_secret``, ``cookie``, ``private_key``
#: on one side; ``credential`` on the other).
SENSITIVE_PARAM_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_key",
        "access_token",
        "refresh_token",
        "token",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "authorization",
        "credential",
        "credentials",
        "cookie",
        "private_key",
    }
)

#: Values that are not credentials but are just as unfit for a log: executable
#: content and templates can carry a secret, and a tool's ``content`` argument
#: is often a whole file.
SENSITIVE_PARAM_NAMES = SENSITIVE_PARAM_KEYS | frozenset(
    {"python_function", "command_template", "content"}
)


def mask_sensitive_params(params: Any) -> Any:
    """A redacted copy for logs and ledger records.

    Non-mapping input becomes a truncated ``repr``; sensitive values become
    ``<masked len=N>``.  This lives here (not in ``engine.callbacks``) because
    the kernel records tool parameters too and must not import the engine.
    """

    if not isinstance(params, dict):
        text = repr(params)
        return text if len(text) <= 200 else text[:200] + "...(truncated)"
    out: dict[Any, Any] = {}
    for key, value in params.items():
        if isinstance(key, str) and key.lower() in SENSITIVE_PARAM_NAMES:
            out[key] = f"<masked len={len(value)}>" if isinstance(value, str) else "<masked>"
        else:
            out[key] = value
    return out

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Provider key shapes: a recognisable prefix plus a long body.
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    # ``12`` is deliberate: the memory guard refuses to persist anything this
    # matches, and the merged list must not be weaker than the list it
    # replaced (it caught 12-char sk-/ak- keys; a 16-char threshold silently
    # let them through into stored memories).
    (re.compile(r"\b(?:sk|ak)-[A-Za-z0-9_-]{12,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), REDACTED),
    (
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
            r"[\s\S]*?-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
        ),
        REDACTED,
    ),
    # Scheme-prefixed auth material, before the name=value rule: otherwise an
    # ``Authorization: Bearer x`` line would leave the scheme word behind.
    (
        re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
        r"\1 " + REDACTED,
    ),
    # name=value where the name itself says it is a secret.  Keeps the name so
    # the reader still knows which credential was removed.  This also covers a
    # sensitive query parameter (``?token=``); ``&`` and ``#`` end the value so
    # one credential cannot swallow the rest of a URL.
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_.-]*"
            r"(?:api[_-]?key|apikey|access[_-]?key|secret|token"
            r"|password|passwd|credential|authorization))"
            r"\b(\s*[:=]\s*)([\"']?)([^\s,;\"'}&#]+)([\"']?)"
        ),
        r"\1\2\3" + REDACTED + r"\5",
    ),
    # Chinese labels, which the ASCII patterns above never see.
    (re.compile(r"(密码|密钥|令牌)\s*[:：=]\s*\S+"), r"\1=" + REDACTED),
)


def redact_text(text: str) -> str:
    """Replace recognisable credentials in ``text`` with ``[redacted]``.

    Structure is preserved: only the secret value changes, so a log line stays
    readable and a diff still lines up.
    """

    if not isinstance(text, str):
        text = str(text)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def contains_secret(text: str) -> bool:
    """Does ``text`` contain something that looks like a credential?"""

    if not isinstance(text, str):
        text = str(text)
    return any(pattern.search(text) for pattern, _ in _PATTERNS)


__all__ = [
    "REDACTED",
    "SENSITIVE_PARAM_KEYS",
    "SENSITIVE_PARAM_NAMES",
    "contains_secret",
    "mask_sensitive_params",
    "redact_text",
]
