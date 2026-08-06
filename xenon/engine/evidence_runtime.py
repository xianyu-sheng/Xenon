"""Structured evidence runtime for Xenon's complete task lifecycle.

This module is deliberately independent from LLMs and tool execution. It provides
an append-only, hash-chained protocol for claims, observations, gate verdicts,
operations, state snapshots, and validation results. Adapters can emit events at
any lifecycle boundary without coupling engines to persistence details.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Iterable


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class LifecyclePhase(_StringEnum):
    TASK = "task"
    UNDERSTANDING = "understanding"
    PLANNING = "planning"
    PRE_TOOL = "pre_tool"
    EXECUTION = "execution"
    POST_TOOL = "post_tool"
    VALIDATION = "validation"
    DELIVERY = "delivery"


class EventKind(_StringEnum):
    TASK_FACT = "task_fact"
    CLAIM = "claim"
    PLAN = "plan"
    TOOL_REQUEST = "tool_request"
    TOOL_OBSERVATION = "tool_observation"
    STATE_SNAPSHOT = "state_snapshot"
    GATE_VERDICT = "gate_verdict"
    VALIDATION_RESULT = "validation_result"
    DELIVERY = "delivery"


class EvidenceSource(_StringEnum):
    USER = "user"
    LLM = "llm"
    TOOL = "tool"
    FILESYSTEM = "filesystem"
    TEST = "test"
    GATE = "gate"
    ENGINE = "engine"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=repr)


def _enum_value(value: str | Enum, enum_type: type[Enum]) -> str:
    raw = value.value if isinstance(value, Enum) else value
    try:
        return enum_type(raw).value
    except ValueError as exc:
        raise ValueError(f"invalid {enum_type.__name__}: {raw}") from exc


@dataclass(slots=True)
class EvidenceEvent:
    event_id: str
    session_id: str
    sequence: int
    phase: str
    kind: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    predecessor_id: str | None = None
    content_hash: str = ""

    @classmethod
    def create(
        cls, *, session_id: str, phase: str | LifecyclePhase,
        kind: str | EventKind, source: str | EvidenceSource,
        payload: dict[str, Any] | None = None, sequence: int = 0,
        predecessor_id: str | None = None,
    ) -> "EvidenceEvent":
        phase_value = _enum_value(phase, LifecyclePhase)
        kind_value = _enum_value(kind, EventKind)
        source_value = _enum_value(source, EvidenceSource)
        if not session_id:
            raise ValueError("session_id must not be empty")
        event = cls(uuid.uuid4().hex, session_id, sequence, phase_value, kind_value,
                    source_value, copy.deepcopy(payload or {}), predecessor_id)
        event.content_hash = event._calculate_hash()
        return event

    def _calculate_hash(self) -> str:
        body = {
            "event_id": self.event_id, "session_id": self.session_id,
            "sequence": self.sequence, "phase": self.phase, "kind": self.kind,
            "source": self.source, "payload": self.payload,
            "predecessor_id": self.predecessor_id,
        }
        return hashlib.sha256(_canonical(body).encode()).hexdigest()

    def verify_integrity(self) -> bool:
        return bool(self.content_hash) and self.content_hash == self._calculate_hash()

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "session_id": self.session_id,
            "sequence": self.sequence, "phase": self.phase, "kind": self.kind,
            "source": self.source, "payload": copy.deepcopy(self.payload),
            "predecessor_id": self.predecessor_id, "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceEvent":
        required = {"event_id", "session_id", "sequence", "phase", "kind", "source", "payload", "content_hash"}
        if not isinstance(data, dict) or not required.issubset(data):
            raise ValueError("invalid evidence event")
        event = cls.create(session_id=str(data["session_id"]), phase=data["phase"],
                           kind=data["kind"], source=data["source"], payload=data["payload"],
                           sequence=int(data["sequence"]), predecessor_id=data.get("predecessor_id"))
        event.event_id = str(data["event_id"])
        event.content_hash = str(data["content_hash"])
        return event


class EvidenceLedger:
    """Hash-chained, session-scoped evidence stream."""

    def __init__(self, session_id: str, events: Iterable[EvidenceEvent] | None = None) -> None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        self.session_id = session_id
        self.events: list[EvidenceEvent] = []
        self._lock = RLock()
        for event in events or ():
            self.append_event(event)

    def append(self, phase: str | LifecyclePhase, kind: str | EventKind,
               source: str | EvidenceSource, payload: dict[str, Any] | None = None) -> EvidenceEvent:
        with self._lock:
            event = EvidenceEvent.create(session_id=self.session_id, phase=phase, kind=kind,
                                         source=source, payload=payload,
                                         sequence=len(self.events) + 1,
                                         predecessor_id=self.events[-1].event_id if self.events else None)
            self.events.append(event)
            return event

    def append_event(self, event: EvidenceEvent) -> EvidenceEvent:
        with self._lock:
            if event.session_id != self.session_id:
                raise ValueError("event session_id does not match ledger")
            expected = len(self.events) + 1
            predecessor = self.events[-1].event_id if self.events else None
            if event.sequence != expected or event.predecessor_id != predecessor:
                raise ValueError("event does not continue ledger chain")
            self.events.append(event)
            return event

    def query(self, *, phase: str | LifecyclePhase | None = None,
              kind: str | EventKind | None = None,
              source: str | EvidenceSource | None = None) -> list[EvidenceEvent]:
        phase_value = _enum_value(phase, LifecyclePhase) if phase is not None else None
        kind_value = _enum_value(kind, EventKind) if kind is not None else None
        source_value = _enum_value(source, EvidenceSource) if source is not None else None
        return [e for e in self.events if (phase_value is None or e.phase == phase_value)
                and (kind_value is None or e.kind == kind_value)
                and (source_value is None or e.source == source_value)]

    def verify_integrity(self) -> bool:
        previous: EvidenceEvent | None = None
        for sequence, event in enumerate(self.events, 1):
            if event.session_id != self.session_id or event.sequence != sequence:
                return False
            if event.predecessor_id != (previous.event_id if previous else None):
                return False
            if not event.verify_integrity():
                return False
            previous = event
        return True

    def snapshot(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "events": [e.to_dict() for e in self.events]}

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> "EvidenceLedger":
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("events"), list):
            raise ValueError("invalid evidence ledger snapshot")
        ledger = cls(
            str(snapshot.get("session_id", "")),
            (EvidenceEvent.from_dict(item) for item in snapshot["events"]),
        )
        if not ledger.verify_integrity():
            raise ValueError("evidence ledger integrity check failed")
        return ledger

    def write_jsonl(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for event in self.events:
                handle.write(_canonical(event.to_dict()) + "\n")

    @classmethod
    def read_jsonl(cls, path: str | Path) -> "EvidenceLedger":
        events: list[EvidenceEvent] = []
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    try:
                        events.append(EvidenceEvent.from_dict(json.loads(line)))
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise ValueError("invalid evidence event") from exc
        if not events:
            raise ValueError("evidence ledger is empty")
        return cls.from_snapshot({"session_id": events[0].session_id,
                                  "events": [e.to_dict() for e in events]})


@dataclass(frozen=True, slots=True)
class EvidencePack:
    session_id: str
    event_count: int
    gate_failures: int
    integrity_verified: bool
    events: tuple[dict[str, Any], ...]

    @classmethod
    def build(cls, ledger: EvidenceLedger) -> "EvidencePack":
        integrity = ledger.verify_integrity()
        if not integrity:
            raise ValueError("cannot build EvidencePack from an invalid ledger")
        failures = sum(1 for e in ledger.events if e.kind == EventKind.GATE_VERDICT.value
                       and e.payload.get("passed") is False)
        return cls(ledger.session_id, len(ledger.events), failures, True,
                   tuple(e.to_dict() for e in ledger.events))

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "event_count": self.event_count,
                "gate_failures": self.gate_failures,
                "integrity_verified": self.integrity_verified, "events": list(self.events)}
