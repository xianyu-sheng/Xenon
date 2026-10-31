"""Deterministic repetition guard for the loop.

The kernel has no interest in "the model looks stuck"; it only reacts to an
observable, countable fact: the same tool intent, with the same arguments,
issued again and again.  That is a cheap signal with no model in the loop,
which is exactly what a guard should be.

The guard never decides outcomes by itself.  It reports that a call repeats
too often; :class:`~xenon.kernel.runtime.LoopRuntime` turns that into a
policy veto plus a blocked result, so the reason lands in the ledger.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Mapping

__all__ = ["RepeatedCallGuard", "call_signature"]

# Identical calls allowed before the guard trips.  Repeating a read once is
# normal (files change); three identical calls in a row means the loop is
# burning budget without new information.
DEFAULT_MAX_IDENTICAL = 3


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def call_signature(intent: str, args: Mapping[str, Any] | None) -> str:
    """Stable identity for one tool call, independent of dict ordering."""

    payload = {"intent": str(intent or ""), "args": dict(args or {})}
    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return digest[:16]


class RepeatedCallGuard:
    """Counts identical calls and reports when one crosses the limit."""

    def __init__(self, max_identical: int = DEFAULT_MAX_IDENTICAL) -> None:
        if max_identical < 1:
            raise ValueError("max_identical must be >= 1")
        self._limit = int(max_identical)
        self._counts: Counter[str] = Counter()
        self._last = ""

    @property
    def limit(self) -> int:
        return self._limit

    def register(self, signature: str) -> bool:
        """Record one call; ``True`` means the guard has tripped."""

        self._counts[signature] += 1
        self._last = signature
        return self._counts[signature] > self._limit

    def count(self, signature: str) -> int:
        return self._counts[signature]

    def reset(self) -> None:
        self._counts.clear()
        self._last = ""

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"RepeatedCallGuard(limit={self._limit}, calls={sum(self._counts.values())})"
