"""One error shape for failures that cross a boundary (doc §13.2 #14).

The protocol, the CLI and the kernel each used to invent their own wording:
a timeout could surface as a provider string, a traceback, or a Chinese
sentence, and a caller could not tell "retry me" from "do not bother".  This
module decides that once, from the exception itself, and every boundary
(front-end frame, CLI exit, result file) renders the same envelope.

It classifies by *behaviour* (permission-denied, timeout, connection, bad
input), not by vendor or message text, so a new provider or tool needs no new
case here.
"""

from __future__ import annotations

import errno
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ErrorEnvelope:
    """The one error shape: what failed, whether it is worth retrying, why."""

    kind: str
    code: str
    message: str
    retryable: bool = False
    cause: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.cause:
            payload["cause"] = self.cause
        if self.details:
            payload["details"] = dict(self.details)
        return payload


_NETWORK_ERRNOS = frozenset(
    {
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
        errno.ETIMEDOUT,
        errno.EPIPE,
    }
)


def _kind_and_retry(exc: BaseException) -> tuple[str, bool]:
    explicit = str(getattr(exc, "kind", "") or "")
    if explicit:
        return explicit, bool(getattr(exc, "retryable", False))
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return "cancelled", False
    if isinstance(exc, PermissionError):
        return "permission", False
    if isinstance(exc, TimeoutError):
        return "network", True
    if isinstance(exc, ConnectionError):
        return "network", True
    if isinstance(exc, OSError):
        retryable = getattr(exc, "errno", None) in _NETWORK_ERRNOS
        return ("network", True) if retryable else ("tool", False)
    name = type(exc).__name__.casefold()
    if "truncat" in name or "ratelimit" in name or "rate_limit" in name:
        return "llm", True
    if "tool" in name:
        return "tool", False
    if "ledger" in name:
        return "integrity", False
    if isinstance(exc, (ValueError, TypeError, KeyError, AttributeError, IndexError)):
        return "internal", False
    return "internal", False


def envelope(
    exc: BaseException | str,
    *,
    code: str = "",
    retryable: bool | None = None,
    details: dict[str, Any] | None = None,
) -> ErrorEnvelope:
    """Classify one failure.  ``retryable``/``code`` may be forced by a caller."""

    if isinstance(exc, str):
        return ErrorEnvelope(
            kind="internal",
            code=code or "message",
            message=exc,
            retryable=bool(retryable),
            details=dict(details or {}),
        )
    kind, guess = _kind_and_retry(exc)
    message = str(exc) or type(exc).__name__
    return ErrorEnvelope(
        kind=kind,
        code=code or type(exc).__name__,
        message=message[:2000],
        retryable=guess if retryable is None else bool(retryable),
        cause=type(exc).__module__,
        details=dict(details or {}),
    )


__all__ = ["ErrorEnvelope", "envelope"]
