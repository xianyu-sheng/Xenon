"""Shared wall-clock budget and retry policy for an engine graph.

The policy is deliberately shared by reference.  A planner, reactor,
reflector, and any later-created sub-engine therefore consume the same
monotonic deadline instead of each receiving a fresh per-request timeout.
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator


class EngineDeadlineExceeded(TimeoutError):
    """Raised before starting work that cannot fit in the run deadline."""


EventSink = Callable[[str, dict[str, Any]], None]


@dataclass(slots=True)
class ExecutionPolicy:
    """One retry owner and one absolute deadline for an engine run.

    ``provider_attempts`` is passed to the low-level provider client.
    ``chain_retries`` belongs to :class:`BaseEngine`.  Callers should enable
    only one of them; benchmark runs use one provider attempt and no chain
    retry so a request cannot expand multiplicatively.
    """

    deadline_at: float | None
    request_timeout: float = 120.0
    provider_attempts: int = 1
    chain_retries: int = 0
    min_request_interval: float = 0.0
    event_sink: EventSink | None = None
    _next_request_at: float = field(default=0.0, init=False, repr=False)
    _request_lock: Any = field(
        default_factory=threading.Lock, init=False, repr=False
    )

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
    ) -> "ExecutionPolicy":
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        return cls(
            deadline_at=time.monotonic() + timeout,
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

    def check(self, phase: str = "engine") -> None:
        remaining = self.remaining()
        if remaining is not None and remaining <= 0:
            self.emit("deadline_exceeded", phase=phase)
            raise EngineDeadlineExceeded(
                f"engine deadline exceeded before {phase}"
            )

    def request_budget(self, phase: str = "provider_request") -> float:
        remaining = self.remaining()
        if remaining is None:
            return self.request_timeout
        if remaining <= 0:
            self.emit("deadline_exceeded", phase=phase)
            raise EngineDeadlineExceeded(
                f"engine deadline exceeded before {phase}"
            )
        budget = min(self.request_timeout, remaining)
        # Avoid handing httpx a zero timeout due to clock granularity.
        return max(0.001, budget)

    def wait_for_request_slot(self, phase: str = "provider_request") -> None:
        """Pace a shared engine graph without multiplying provider retries.

        Combined engines share this policy object, so planner, reactor and
        reflector requests reserve one monotonic sequence of start slots.  The
        default interval is zero; benchmark adapters can opt into pacing when
        a provider enforces a low rolling RPM limit.
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
                raise EngineDeadlineExceeded(
                    "engine deadline would be exceeded during provider pacing"
                )
            self._next_request_at = scheduled + self.min_request_interval
        if delay > 0:
            self.emit(
                "provider_pacing",
                phase=phase,
                delay_seconds=round(delay, 3),
            )
            self.sleep(delay, phase="provider_pacing")

    def sleep(self, seconds: float, phase: str = "retry_backoff") -> None:
        remaining = self.remaining()
        if remaining is not None and remaining <= 0:
            self.emit("deadline_exceeded", phase=phase)
            raise EngineDeadlineExceeded(
                f"engine deadline exceeded before {phase}"
            )
        if remaining is not None and seconds >= remaining:
            self.emit("deadline_exceeded", phase=phase)
            raise EngineDeadlineExceeded(
                f"engine deadline would be exceeded during {phase}"
            )
        time.sleep(max(0.0, seconds))


_ENGINE_CHILDREN = (
    "planner",
    "reactor",
    "repairer",
    "reflector",
    "executor",
    "_last_subagent",
)


def walk_engine_graph(engine: Any) -> Iterator[Any]:
    """Yield every current engine node exactly once, including reflectors."""

    pending = [engine]
    seen: set[int] = set()
    while pending:
        node = pending.pop(0)
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        for name in _ENGINE_CHILDREN:
            child = getattr(node, name, None)
            if child is not None and not isinstance(
                child, (str, bytes, list, tuple, dict, set)
            ):
                pending.append(child)


def bind_execution_policy(engine: Any, policy: ExecutionPolicy) -> None:
    """Bind the same policy object to a complete, already-built engine graph."""

    for node in walk_engine_graph(engine):
        node.execution_policy = policy
        # Compatibility for integrations that still inspect request_timeout.
        node.request_timeout = policy.request_timeout
        executor = getattr(node, "_tool_executor", None)
        if executor is not None:
            executor.set_execution_policy(policy)
