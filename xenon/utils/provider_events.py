"""Provider-phase events: what the model call is doing, per attempt (#320).

Before this, ``provider_request_start`` / ``provider_request_end`` were declared
on ``EngineCallback`` but nothing emitted them: the emitter lived in the engine
layer and was deleted with it.  So the REPL could say "generating, 12s" but never
"retrying after a 429" or "timed out on attempt 2 of 3".

This replaced `EngineCallback.on_provider_request_start/end`.  Those hooks were
declared but never called by anything once the engine layer was deleted, and the
REPL's own `ConsoleCallback` never implemented them — dead API that looked alive.
They are gone; this sink is the only provider-phase mechanism.

Deliberately **not** an event bus.  A bus is global mutable state shared by every
concurrent run; the kernel already keeps per-call hooks on ``ModelConfig``
(``usage_sink``, ``should_cancel``, ``progress_sink``) precisely so parallel child
tasks cannot see each other's signals.  This is one more callable in the same
shape.
"""

from __future__ import annotations

from typing import Any

START = "start"
END = "end"

__all__ = ["END", "START", "emit", "request_start", "request_end"]


def emit(sink: Any, payload: dict[str, Any]) -> None:
    """Call a request sink, swallowing its failures.

    Telemetry must never be able to fail a model call: the operator's spinner
    breaking is not a reason to lose a run.
    """

    if sink is None:
        return
    try:
        sink(payload)
    except Exception:  # noqa: BLE001 — telemetry is never load-bearing
        pass


def request_start(
    sink: Any,
    *,
    phase: str,
    timeout: float,
    provider_attempts: int,
    attempt: int,
    streamed: bool = False,
) -> None:
    """Report that one HTTP request is about to be sent."""

    emit(
        sink,
        {
            "kind": START,
            "phase": phase,
            "timeout": float(timeout),
            "provider_attempts": int(provider_attempts),
            "attempt": int(attempt),
            "streamed": bool(streamed),
        },
    )


def request_end(
    sink: Any,
    *,
    phase: str,
    success: bool,
    attempt: int,
    retrying: bool = False,
    reason: str = "",
) -> None:
    """Report one attempt's outcome (not the whole call's)."""

    emit(
        sink,
        {
            "kind": END,
            "phase": phase,
            "success": bool(success),
            "attempt": int(attempt),
            "retrying": bool(retrying),
            "reason": str(reason)[:200],
        },
    )
