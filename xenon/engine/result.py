"""Uniform engine outcome metadata.

Engines still return a string-compatible answer, so existing callers keep
working.  ``AgentResult`` is a ``str`` subclass carrying *why* the run ended
and the execution facts that produced it.  The moment a caller needs to
distinguish completion from loop/deadline/delivery failure, it can read the
metadata instead of parsing warning text.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from xenon.engine.task_state import TaskState


class TerminationReason(str, Enum):
    """Closed set of reasons an engine run can end."""

    FINAL = "final"
    MAX_STEPS = "max_steps"
    DEADLINE = "deadline"
    LOOP = "loop"
    NO_PROGRESS = "no_progress"
    USER_INTERRUPT = "user_interrupt"
    DELIVERY_INCOMPLETE = "delivery_incomplete"
    ERROR = "error"


class AgentResult(str):
    """String answer plus structured termination/execution metadata."""

    def __new__(
        cls,
        answer: str,
        *,
        reason: TerminationReason | str = TerminationReason.FINAL,
        iterations: int = 0,
        tool_calls: int = 0,
        artifacts: list[str] | tuple[str, ...] = (),
        tests: list[str] | tuple[str, ...] = (),
        commit: str = "",
        missing_delivery: list[str] | tuple[str, ...] = (),
        engine: str = "",
    ) -> "AgentResult":
        obj = super().__new__(cls, str(answer))
        obj.reason = (
            reason if isinstance(reason, TerminationReason) else TerminationReason(str(reason))
        )
        obj.iterations = max(0, int(iterations))
        obj.tool_calls = max(0, int(tool_calls))
        obj.artifacts = tuple(str(item) for item in artifacts)
        obj.tests = tuple(str(item) for item in tests)
        obj.commit = str(commit or "")
        obj.missing_delivery = tuple(str(item) for item in missing_delivery)
        obj.engine = str(engine or "")
        return obj

    @property
    def ok(self) -> bool:
        """True only for a normal final answer with no missing delivery."""

        return (
            self.reason is TerminationReason.FINAL
            and not self.missing_delivery
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": str(self),
            "reason": self.reason.value,
            "ok": self.ok,
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "artifacts": list(self.artifacts),
            "tests": list(self.tests),
            "commit": self.commit,
            "missing_delivery": list(self.missing_delivery),
            "engine": self.engine,
        }

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"AgentResult(reason={self.reason.value!r}, ok={self.ok!r}, "
            f"iterations={self.iterations}, tool_calls={self.tool_calls}, "
            f"answer={str(self)[:80]!r})"
        )


def build_agent_result(
    engine: Any,
    answer: str,
    context: Any | None = None,
    *,
    reason: TerminationReason | str | None = None,
) -> AgentResult:
    """Build a result from the engine's latest tracker and TaskState."""

    state = TaskState.from_context(context)
    missing = state.missing_delivery() if state and state.active else []
    resolved = reason or getattr(engine, "_last_termination_reason", None)
    if resolved is None:
        resolved = (
            TerminationReason.DELIVERY_INCOMPLETE
            if missing
            else TerminationReason.FINAL
        )
    resolved = (
        resolved
        if isinstance(resolved, TerminationReason)
        else TerminationReason(str(resolved))
    )
    tracker = getattr(engine, "_last_tracker", None)
    calls = list(getattr(tracker, "calls", []) or []) if tracker is not None else []
    iterations = getattr(engine, "_last_iterations", 0) or 0
    return AgentResult(
        answer,
        reason=resolved,
        iterations=iterations,
        tool_calls=len(calls),
        artifacts=state.artifacts if state else (),
        tests=state.tests if state else (),
        commit=state.commit if state else "",
        missing_delivery=missing,
        engine=type(engine).__name__,
    )
