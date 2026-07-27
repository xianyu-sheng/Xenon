"""Append-only conversation events and per-model prompt cache lanes.

The provider owns the actual prompt cache.  Xenon can nevertheless guarantee
that requests sent to one compatible model/engine contract are append-only and
measure how much of the next request is eligible for provider-side reuse.

Two structures deliberately coexist:

``SessionEventLog``
    A provider-neutral, immutable audit stream for the whole conversation.

``PromptLaneRegistry``
    In-memory request fingerprints grouped by model and request contract.  It
    never stores credentials and only retains message hashes, not prompt text.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from xenon.utils.token_estimate import estimate_text_tokens


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _freeze(value: Any) -> Any:
    """Convert metadata into an immutable, deterministic representation."""
    if isinstance(value, Mapping):
        return tuple((str(key), _freeze(value[key])) for key in sorted(value, key=str))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return str(value)


def _message_fingerprint(message: Mapping[str, Any]) -> str:
    return _digest(message)


def _estimated_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    return sum(estimate_text_tokens(_canonical_json(message)) for message in messages)


@dataclass(frozen=True)
class SessionEvent:
    """One immutable event in the provider-neutral conversation stream."""

    event_id: int
    epoch: int
    kind: str
    role: str
    content: str
    model_id: str | None
    metadata: tuple[tuple[str, Any], ...]
    timestamp: float


class SessionEventLog:
    """Thread-safe append-only session event stream."""

    def __init__(self, *, epoch: int = 0) -> None:
        self._events: list[SessionEvent] = []
        self._next_id = 1
        self._epoch = max(0, int(epoch))
        self._lock = threading.RLock()

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def latest_id(self) -> int:
        with self._lock:
            return self._events[-1].event_id if self._events else 0

    def append(
        self,
        kind: str,
        *,
        role: str = "",
        content: str = "",
        model_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SessionEvent:
        with self._lock:
            frozen = _freeze(dict(metadata or {}))
            event = SessionEvent(
                event_id=self._next_id,
                epoch=self._epoch,
                kind=str(kind),
                role=str(role),
                content=str(content),
                model_id=str(model_id) if model_id else None,
                metadata=frozen,
                timestamp=time.time(),
            )
            self._events.append(event)
            self._next_id += 1
            return event

    def start_epoch(self, epoch: int, reason: str) -> SessionEvent:
        """Advance the structural epoch without deleting earlier evidence."""
        with self._lock:
            requested = max(0, int(epoch))
            if requested <= self._epoch:
                requested = self._epoch + 1
            self._epoch = requested
            return self.append(
                "epoch_started",
                content=str(reason),
                metadata={"epoch": self._epoch},
            )

    def events_since(self, event_id: int = 0) -> tuple[SessionEvent, ...]:
        with self._lock:
            return tuple(event for event in self._events if event.event_id > event_id)

    def snapshot(self) -> tuple[SessionEvent, ...]:
        with self._lock:
            return tuple(self._events)


@dataclass
class PromptLane:
    """The latest exact request prefix for one provider request contract."""

    lane_id: str
    model_id: str
    engine: str
    phase: str
    context_epoch: int
    contract_hash: str
    generation: int = 0
    request_count: int = 0
    last_event_id: int = 0
    last_prompt_hash: str = ""
    message_fingerprints: tuple[str, ...] = ()
    estimated_prompt_tokens: int = 0
    last_used_at: float = 0.0
    fork_reason: str = ""


@dataclass(frozen=True)
class LaneDecision:
    """Cache eligibility result for one outgoing provider request."""

    lane_id: str
    generation: int
    cold_start: bool
    append_only: bool
    reason: str
    request_count: int
    reusable_message_count: int
    reusable_tokens: int
    estimated_prompt_tokens: int
    event_cursor: int

    def cache_context(self) -> dict[str, Any]:
        return {
            "cache_lane": self.lane_id,
            "lane_generation": self.generation,
            "lane_append_only": self.append_only,
            "lane_reason": self.reason,
            "lane_reusable_messages": self.reusable_message_count,
            "lane_reusable_tokens": self.reusable_tokens,
            "lane_prompt_tokens": self.estimated_prompt_tokens,
            "event_cursor": self.event_cursor,
        }


class PromptLaneRegistry:
    """Track exact-prefix continuity independently for every routed model."""

    def __init__(self, *, max_archived_lanes: int = 64) -> None:
        if max_archived_lanes < 0:
            raise ValueError("max_archived_lanes must be non-negative")
        self._active: dict[str, PromptLane] = {}
        self._archive: list[PromptLane] = []
        self._max_archived_lanes = int(max_archived_lanes)
        self._lock = threading.RLock()

    def _archive_lane(self, lane: PromptLane) -> None:
        """Retain only recent forked lanes for bounded diagnostics memory."""
        if self._max_archived_lanes == 0:
            return
        self._archive.append(lane)
        overflow = len(self._archive) - self._max_archived_lanes
        if overflow > 0:
            del self._archive[:overflow]

    @staticmethod
    def _base_key(
        model_id: str,
        engine: str,
        phase: str,
        context_epoch: int,
        contract_hash: str,
    ) -> str:
        material = {
            "model": str(model_id).strip().lower(),
            "engine": str(engine).strip().lower(),
            "phase": str(phase).strip().lower(),
            "context_epoch": max(0, int(context_epoch)),
            "contract_hash": contract_hash,
        }
        return _digest(material)

    @staticmethod
    def contract_hash(
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        request_shape: Mapping[str, Any] | None = None,
    ) -> str:
        """Hash only the immutable leading system contract and tool shape."""
        stable_prefix: list[Mapping[str, Any]] = []
        for message in messages:
            if str(message.get("role", "")) != "system":
                break
            stable_prefix.append(message)
        return _digest({
            "stable_prefix": stable_prefix,
            "tools": list(tools or []),
            "request_shape": dict(request_shape or {}),
        })

    def prepare(
        self,
        model_id: str,
        engine: str,
        phase: str,
        context_epoch: int,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        request_shape: Mapping[str, Any] | None = None,
        event_cursor: int = 0,
    ) -> LaneDecision:
        """Record a request and report whether it extends the previous request.

        An identical retry is considered append-only and fully reusable.  If a
        caller rewrites history, the old lane is archived and a new generation
        begins rather than pretending the provider can reuse a broken prefix.
        """
        fingerprints = tuple(_message_fingerprint(message) for message in messages)
        prompt_tokens = _estimated_tokens(messages)
        contract = self.contract_hash(
            messages,
            tools=tools,
            request_shape=request_shape,
        )
        base_key = self._base_key(model_id, engine, phase, context_epoch, contract)

        with self._lock:
            lane = self._active.get(base_key)
            cold_start = lane is None
            append_only = True
            reusable_count = 0
            reusable_tokens = 0
            reason = "cold_start"

            if lane is None:
                lane = PromptLane(
                    lane_id=base_key[:20],
                    model_id=str(model_id),
                    engine=str(engine),
                    phase=str(phase),
                    context_epoch=max(0, int(context_epoch)),
                    contract_hash=contract,
                )
                self._active[base_key] = lane
            else:
                previous = lane.message_fingerprints
                append_only = (
                    len(fingerprints) >= len(previous)
                    and fingerprints[:len(previous)] == previous
                )
                if append_only:
                    reusable_count = len(previous)
                    reusable_tokens = lane.estimated_prompt_tokens
                    reason = "exact_retry" if fingerprints == previous else "prefix_extended"
                else:
                    self._archive_lane(lane)
                    generation = lane.generation + 1
                    lane = PromptLane(
                        lane_id=f"{base_key[:16]}-g{generation}",
                        model_id=str(model_id),
                        engine=str(engine),
                        phase=str(phase),
                        context_epoch=max(0, int(context_epoch)),
                        contract_hash=contract,
                        generation=generation,
                        fork_reason="history_rewritten",
                    )
                    self._active[base_key] = lane
                    cold_start = True
                    reason = "history_rewritten"

            lane.request_count += 1
            lane.last_event_id = max(0, int(event_cursor))
            lane.last_prompt_hash = _digest(list(messages))
            lane.message_fingerprints = fingerprints
            lane.estimated_prompt_tokens = prompt_tokens
            lane.last_used_at = time.time()
            return LaneDecision(
                lane_id=lane.lane_id,
                generation=lane.generation,
                cold_start=cold_start,
                append_only=append_only,
                reason=reason,
                request_count=lane.request_count,
                reusable_message_count=reusable_count,
                reusable_tokens=reusable_tokens,
                estimated_prompt_tokens=prompt_tokens,
                event_cursor=lane.last_event_id,
            )

    def snapshots(self) -> tuple[dict[str, Any], ...]:
        """Return content-free diagnostics for active and archived lanes."""
        with self._lock:
            lanes = [*self._active.values(), *self._archive]
            return tuple({
                "lane_id": lane.lane_id,
                "model_id": lane.model_id,
                "engine": lane.engine,
                "phase": lane.phase,
                "context_epoch": lane.context_epoch,
                "generation": lane.generation,
                "request_count": lane.request_count,
                "last_event_id": lane.last_event_id,
                "estimated_prompt_tokens": lane.estimated_prompt_tokens,
                "last_used_at": lane.last_used_at,
                "fork_reason": lane.fork_reason,
                "active": lane in self._active.values(),
            } for lane in lanes)

    def model_warmth(
        self,
        model_id: str,
        *,
        engine: str | None = None,
        phase: str | None = None,
        max_age_seconds: float = 30 * 60,
    ) -> dict[str, Any]:
        """Return the warmest active lane for a model without prompt content."""
        with self._lock:
            candidates = [
                lane for lane in self._active.values()
                if lane.model_id.lower() == str(model_id).lower()
                and (engine is None or lane.engine.lower() == str(engine).lower())
                and (phase is None or lane.phase.lower() == str(phase).lower())
            ]
            if not candidates:
                return {"eligible": False, "reusable_tokens": 0, "age_seconds": None}
            lane = max(candidates, key=lambda item: item.last_used_at)
            age = max(0.0, time.time() - lane.last_used_at)
            reusable = lane.estimated_prompt_tokens
            eligible = (
                lane.request_count > 0
                and reusable >= 256
                and age <= max(0.0, float(max_age_seconds))
            )
            return {
                "eligible": eligible,
                "score": round(min(0.5, reusable / 16_384), 4) if eligible else 0.0,
                "reason": "warm_prompt_lane" if eligible else "lane_too_small_or_stale",
                "reusable_tokens": reusable,
                "age_seconds": age,
                "lane_id": lane.lane_id,
                "engine": lane.engine,
                "phase": lane.phase,
            }
