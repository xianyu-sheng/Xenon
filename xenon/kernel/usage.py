"""Token accounting for the loop.

Without this the word "budget" is decorative: a run could spend half a
million tokens and nothing would ever stop it.  Two rules keep it honest:

1. **Real numbers first.**  Providers report ``usage``; we record that.  When
   a provider omits it (streaming, some gateways), we fall back to a character
   estimate and mark the event ``estimated=true`` rather than pretending.
2. **The ledger owns the total.**  Every model call appends one
   ``model_usage`` event; the runtime's running total is just the fold of
   those events, so a replay reconstructs the same number.

The module is deliberately free of kernel imports: it is arithmetic over a
payload plus one subscription helper.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

__all__ = [
    "UsageTotals",
    "usage_payload",
    "estimate_usage",
    "subscribe_usage",
    "USAGE_SOURCE_REPORTED",
    "USAGE_SOURCE_ESTIMATED",
]

USAGE_SOURCE_REPORTED = "reported"
USAGE_SOURCE_ESTIMATED = "estimated"

# Rough token estimate for providers that report nothing.  Coding traffic is
# mostly ASCII with four-chars-per-token; CJK is closer to one.  Averaging at
# three keeps the estimate conservative rather than optimistic.
_CHARS_PER_TOKEN_ESTIMATE = 3


class UsageTotals:
    """Running totals for one task."""

    __slots__ = (
        "prompt",
        "completion",
        "cache_hit",
        "calls",
        "estimated",
        "last_prompt",
        "last_completion",
    )

    def __init__(self) -> None:
        self.prompt = 0
        self.completion = 0
        self.cache_hit = 0
        self.calls = 0
        self.estimated = 0
        #: The *last* call's prompt size.  This is the context depth — what the
        #: model actually had in front of it — as opposed to ``prompt``, which
        #: is the cumulative bill (#408).
        self.last_prompt = 0
        self.last_completion = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def add(self, payload: Mapping[str, Any]) -> None:
        self.calls += 1
        self.last_prompt = int(payload.get("prompt_tokens") or 0)
        self.last_completion = int(payload.get("completion_tokens") or 0)
        self.prompt += self.last_prompt
        self.completion += int(payload.get("completion_tokens") or 0)
        self.cache_hit += int(payload.get("cache_hit_tokens") or 0)
        if payload.get("estimated"):
            self.estimated += 1

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt,
            "completion_tokens": self.completion,
            "total_tokens": self.total,
            "cache_hit_tokens": self.cache_hit,
            "calls": self.calls,
            "estimated_calls": self.estimated,
        }


def estimate_usage(
    *, prompt_chars: int, completion_chars: int, source: str = ""
) -> dict[str, Any]:
    """Fallback accounting when the provider reports nothing."""

    return {
        "model": source,
        "prompt_tokens": max(1, int(prompt_chars) // _CHARS_PER_TOKEN_ESTIMATE),
        "completion_tokens": max(1, int(completion_chars) // _CHARS_PER_TOKEN_ESTIMATE),
        "cache_hit_tokens": 0,
        "estimated": True,
    }


def usage_payload(
    usage: Any, *, model: str = "", latency: float = 0.0
) -> dict[str, Any]:
    """Normalise an ``LLMUsage`` (or a mapping) into one ledger payload."""

    def _get(name: str) -> int:
        if isinstance(usage, Mapping):
            return int(usage.get(name) or 0)
        return int(getattr(usage, name, 0) or 0)

    prompt = _get("prompt_tokens")
    completion = _get("completion_tokens")
    payload: dict[str, Any] = {
        "model": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cache_hit_tokens": _get("cache_hit_tokens"),
        "estimated": not (prompt or completion),
        "latency_seconds": round(float(latency or 0.0), 3),
    }
    return payload


def subscribe_usage(sink: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
    """Subscribe ``sink`` to provider usage reports; returns an unsubscribe.

    Wraps the one global registry in :mod:`xenon.utils.llm_usage` so callers
    never touch that state directly, and so a sink exception cannot break a
    model call.
    """

    from xenon.utils.llm_usage import register_usage_callback

    def _forward(model_id: str, usage: Any, latency: float) -> None:
        try:
            sink(usage_payload(usage, model=model_id, latency=latency))
        except Exception:  # noqa: BLE001 — accounting must never break a call
            pass

    return register_usage_callback(_forward)
