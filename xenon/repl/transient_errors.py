"""Transport-level error classification (#202).

Retry policy is about the network boundary, not about reasoning paradigms, so it
lives next to the REPL instead of inside the (being deleted) engine base class.
"""

from __future__ import annotations

import httpx

_TRANSIENT_TYPES = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
    httpx.WriteError,
    httpx.PoolTimeout,
)


def is_transient_error(error: Exception | None) -> bool:
    """Whether a failure is worth a backoff retry.

    Terminal errors (401/403/400) are never transient.  A truncated response is:
    the same request may succeed on the next attempt.
    """

    if error is None:
        return False
    if type(error).__name__ == "ResponseTruncatedError":
        return True
    if isinstance(error, _TRANSIENT_TYPES):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return status == 429 or 500 <= status < 600
    return False


__all__ = ["is_transient_error"]
