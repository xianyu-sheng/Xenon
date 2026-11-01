"""Shared wall-clock budget and request pacing for one run.

The policy is shared **by reference**: every provider request issued during a
run spends the same monotonic deadline instead of each getting a fresh
timeout, otherwise a loop with N model calls silently gets N times the wall
clock it was told to respect.

This lived in the engine package and was deleted with the engines, but it was
never engine-specific — the SWE-bench adapter still needs it to keep a matrix
of runs inside a per-instance budget.  Hence its own home, free of any engine
imports.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "DeadlineExceeded",
    "RequestBudget",
    "EventSink",
    "DEFAULT_RUN_TIMEOUT",
    "timeout_from_env",
]

EventSink = Callable[[str, dict[str, Any]], None]

# A run must have a finite safety budget even when the caller forgets to bind
# one.  Interactive callers can still pass something shorter.
DEFAULT_RUN_TIMEOUT = 1800.0


class DeadlineExceeded(TimeoutError):
    """Raised before starting work that cannot fit inside the run deadline."""


def timeout_from_env(default: float = DEFAULT_RUN_TIMEOUT) -> float:
    """Resolve the run deadline, honouring ``XENON_ENGINE_TIMEOUT``.

    The variable name is historical and stays for compatibility with existing
    deployments; invalid or non-positive values fall back to the default
    rather than disabling the safety gate.
    """

    raw = os.environ.get("XENON_ENGINE_TIMEOUT", "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(slots=True)
class RequestBudget:
    """One retry owner and one absolute deadline for a run.

    ``provider_attempts`` is passed to the low-level client; ``chain_retries``
    belongs to whoever rotates models.  Enabling both would multiply retries,
    so the constructor rejects it.
    """

    deadline_at: float | None
    timeout_seconds: float | None = None
    request_timeout: float = 120.0
    provider_attempts: int = 1
    chain_retries: int = 0
    min_request_interval: float = 0.0
    event_sink: EventSink | None = None
    _next_request_at: float = field(default=0.0, init=False, repr=False)
    _request_lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if self.provider_attempts < 1:
            raise ValueError("provider_attempts must be at least 1")
        if self.chain_retries < 0:
            raise ValueError("chain_retries must not be negative")
        if self.min_request_interval < 0:
            raise ValueError("min_request_interval must not be negative")
        if self.provider_attempts > 1 and self.chain_retries > 0:
            raise ValueError(
                "provider_attempts and chain_retries cannot both own retries"
            )

    @classmethod
    def from_timeout(
        cls,
        timeout: float,
        *,
        request_timeout: float | None = None,
        provider_attempts: int = 1,
        chain_retries: int = 0,
        min_request_interval: float = 0.0,
        event_sink: EventSink | None = None,
    ) -> "RequestBudget":
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        return cls(
            deadline_at=time.monotonic() + timeout,
            timeout_seconds=timeout,
            request_timeout=request_timeout or timeout,
            provider_attempts=provider_attempts,
            chain_retries=chain_retries,
            min_request_interval=min_request_interval,
            event_sink=event_sink,
        )

    def emit(self, event: str, **fields: Any) -> None:
        if self.event_sink is not None:
            self.event_sink(event, fields)

    def remaining(self) -> float | None:
        if self.deadline_at is None:
            return None
        return max(0.0, self.deadline_at - time.monotonic())

    def check(self, phase: str = "run") -> None:
        remaining = self.remaining()
        if remaining is not None and remaining <= 0:
            self.emit("deadline_exceeded", phase=phase)
            raise DeadlineExceeded(f"run deadline exceeded before {phase}")

    def request_budget(self, phase: str = "provider_request") -> float:
        remaining = self.remaining()
        if remaining is None:
            return self.request_timeout
        if remaining <= 0:
            self.emit("deadline_exceeded", phase=phase)
            raise DeadlineExceeded(f"run deadline exceeded before {phase}")
        # Never hand httpx a zero timeout because of clock granularity.
        return max(0.001, min(self.request_timeout, remaining))

    def wait_for_request_slot(self, phase: str = "provider_request") -> None:
        """Reserve the next request start slot, optionally paced.

        The default interval is zero; benchmark adapters opt into pacing when a
        provider enforces a low rolling RPM limit.  Sharing one policy object
        across a run therefore yields one monotonic sequence of start slots.
        """

        if self.min_request_interval <= 0:
            self.check(phase)
            return
        with self._request_lock:
            now = time.monotonic()
            scheduled = max(now, self._next_request_at)
            delay = scheduled - now
            remaining = self.remaining()
            if remaining is not None and delay >= remaining:
                self.emit("deadline_exceeded", phase="provider_pacing")
                raise DeadlineExceeded(
                    "run deadline would be exceeded during provider pacing"
                )
            self._next_request_at = scheduled + self.min_request_interval
        if delay > 0:
            self.emit("provider_pacing", phase=phase, delay_seconds=round(delay, 3))
            self.sleep(delay, phase="provider_pacing")

    def sleep(self, seconds: float, phase: str = "retry_backoff") -> None:
        remaining = self.remaining()
        if remaining is not None and remaining <= 0:
            self.emit("deadline_exceeded", phase=phase)
            raise DeadlineExceeded(f"run deadline exceeded before {phase}")
        if remaining is not None and seconds >= remaining:
            self.emit("deadline_exceeded", phase=phase)
            raise DeadlineExceeded(f"run deadline would be exceeded during {phase}")
        time.sleep(max(0.0, seconds))
