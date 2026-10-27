"""Typed ledger events and content-addressed fact IDs.

The ledger is the only durable source of truth.  Events carry *facts* produced
by the gateway; claims and hypotheses are explicitly separate ID spaces so a
model proposal can never be mistaken for a fact.

This module is intentionally independent of the legacy engine.  It imports the
kernel contracts only.
"""

from __future__ import annotations

import copy
import hashlib
import hmac as hmac_lib
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from xenon.kernel.contracts import (
    Effect,
    FsDelete,
    FsWrite,
    NetworkFact,
    Opaque,
    ReadFact,
    TestRun,
    ToolReceipt,
    VcsCommit,
    VcsPush,
)

SCHEMA_VERSION = "ledger/v1"


class EventKind(str, Enum):
    TASK_STARTED = "task_started"
    INTAKE_RECORDED = "intake_recorded"
    PLAN_PROPOSED = "plan_proposed"
    PLAN_ACCEPTED = "plan_accepted"
    PLAN_UPDATED = "plan_updated"
    STEP_STARTED = "step_started"
    STEP_VERDICT = "step_verdict"
    STEP_CLOSED = "step_closed"
    TOOL_REQUESTED = "tool_requested"
    TOOL_RECEIPT = "tool_receipt"
    CLAIM_PROPOSED = "claim_proposed"
    CLAIM_RESOLVED = "claim_resolved"
    DEBT_OPENED = "debt_opened"
    DEBT_CLOSED = "debt_closed"
    AUDIT_REPORTED = "audit_reported"
    DELIVERY_ATTEMPTED = "delivery_attempted"
    DELIVERED = "delivered"
    FINALIZE_STARTED = "finalize_started"
    FINALIZED = "finalized"
    STATE_TRANSITION = "state_transition"
    CONTEXT_COMPACTED = "context_compacted"
    THOUGHT = "thought"
    POLICY_DECISION = "policy_decision"
    CONTROL_DECISION = "control_decision"
    BINDING_FAILED = "binding_failed"
    USER_QUESTION = "user_question"
    USER_INPUT = "user_input"
    USER_APPROVAL = "user_approval"
    INTERRUPT = "interrupt"
    LEGACY_SESSION = "legacy_session"


class EventSource(str, Enum):
    USER = "user"
    LLM = "llm"
    TOOL = "tool"
    GATE = "gate"
    RUNTIME = "runtime"
    FILESYSTEM = "filesystem"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _content_id(prefix: str, value: Any) -> str:
    return f"{prefix}-{_digest(value)[:16]}"


def fact_id(value: Any) -> str:
    """Content-addressed ID for a gateway fact (an effect)."""

    return _content_id("fact", value)


def claim_id(value: Any) -> str:
    """Content-addressed ID for a model/user proposal.  Never a fact ID."""

    return _content_id("claim", value)


def hypothesis_id(value: Any) -> str:
    return _content_id("hyp", value)


def effect_to_payload(effect: Effect) -> dict[str, Any]:
    if isinstance(effect, FsWrite):
        return {
            "type": "fs_write",
            "paths": list(effect.paths),
            "hashes": dict(effect.hashes),
            "changed": effect.changed,
            "created_paths": list(effect.created_paths),
        }
    if isinstance(effect, FsDelete):
        return {
            "type": "fs_delete",
            "paths": list(effect.paths),
            "changed": effect.changed,
        }
    if isinstance(effect, TestRun):
        return {
            "type": "test_run",
            "target": effect.target,
            "framework": effect.framework,
            "tests_run": effect.tests_run,
            "passed": effect.passed,
            "failed": effect.failed,
            "errors": effect.errors,
            "skipped": effect.skipped,
            "exit_code": effect.exit_code,
            "command": effect.command,
        }
    if isinstance(effect, VcsCommit):
        return {
            "type": "vcs_commit",
            "sha": effect.sha,
            "branch": effect.branch,
            "files": list(effect.files),
        }
    if isinstance(effect, VcsPush):
        return {
            "type": "vcs_push",
            "remote": effect.remote,
            "branch": effect.branch,
            "sha": effect.sha,
        }
    if isinstance(effect, ReadFact):
        return {
            "type": "read",
            "path": effect.path,
            "content_hash": effect.content_hash,
            "content": effect.content,
        }
    if isinstance(effect, NetworkFact):
        return {
            "type": "network",
            "url": effect.url,
            "status": effect.status,
            "content": effect.content,
        }
    if isinstance(effect, Opaque):
        return {
            "type": "opaque",
            "command": effect.command,
            "exit_code": effect.exit_code,
            "workspace_delta": list(effect.workspace_delta),
        }
    raise TypeError(f"unknown effect: {type(effect).__name__}")


def effect_from_payload(data: Mapping[str, Any]) -> Effect:
    kind = data.get("type")
    if kind == "fs_write":
        return FsWrite(
            paths=tuple(data.get("paths") or ()),
            hashes=dict(data.get("hashes") or {}),
            changed=bool(data.get("changed", True)),
            created_paths=tuple(data.get("created_paths") or ()),
        )
    if kind == "fs_delete":
        return FsDelete(
            paths=tuple(data.get("paths") or ()),
            changed=bool(data.get("changed", True)),
        )
    if kind == "test_run":
        return TestRun(
            target=str(data.get("target") or ""),
            framework=str(data.get("framework") or "pytest"),
            tests_run=int(data.get("tests_run") or 0),
            passed=int(data.get("passed") or 0),
            failed=int(data.get("failed") or 0),
            errors=int(data.get("errors") or 0),
            skipped=int(data.get("skipped") or 0),
            exit_code=int(data.get("exit_code") or 0),
            command=str(data.get("command") or ""),
        )
    if kind == "vcs_commit":
        return VcsCommit(
            sha=str(data.get("sha") or ""),
            branch=str(data.get("branch") or ""),
            files=tuple(data.get("files") or ()),
        )
    if kind == "vcs_push":
        return VcsPush(
            remote=str(data.get("remote") or ""),
            branch=str(data.get("branch") or ""),
            sha=str(data.get("sha") or ""),
        )
    if kind == "read":
        return ReadFact(
            path=str(data.get("path") or ""),
            content_hash=str(data.get("content_hash") or ""),
            content=str(data.get("content") or ""),
        )
    if kind == "network":
        return NetworkFact(
            url=str(data.get("url") or ""),
            status=int(data.get("status") or 0),
            content=str(data.get("content") or ""),
        )
    if kind == "opaque":
        return Opaque(
            command=str(data.get("command") or ""),
            exit_code=int(data.get("exit_code") or 0),
            workspace_delta=tuple(data.get("workspace_delta") or ()),
        )
    raise ValueError(f"unknown effect payload: {kind!r}")


def receipt_to_payload(receipt: ToolReceipt) -> dict[str, Any]:
    return {
        "receipt_id": receipt.receipt_id,
        "call_id": receipt.call_id,
        "task_id": receipt.task_id,
        "step_id": receipt.step_id,
        "tool": receipt.tool,
        "status": receipt.status.value,
        "exit_code": receipt.exit_code,
        "effects": [effect_to_payload(effect) for effect in receipt.effects],
        "declared_effects": [item.value for item in receipt.declared_effects],
        "observed_effects": [item.value for item in receipt.observed_effects],
        "raw_ref": receipt.raw_ref,
        "error": receipt.error,
        "started_at": receipt.started_at,
        "ended_at": receipt.ended_at,
    }


def receipt_from_payload(data: Mapping[str, Any]) -> ToolReceipt:
    from xenon.kernel.contracts import EffectKind, ReceiptStatus

    return ToolReceipt(
        receipt_id=str(data.get("receipt_id") or ""),
        call_id=str(data.get("call_id") or ""),
        task_id=str(data.get("task_id") or ""),
        step_id=data.get("step_id"),
        tool=str(data.get("tool") or ""),
        status=ReceiptStatus(str(data.get("status") or "ok")),
        exit_code=data.get("exit_code"),
        effects=tuple(
            effect_from_payload(item) for item in (data.get("effects") or ())
        ),
        declared_effects=tuple(
            EffectKind(item) for item in (data.get("declared_effects") or ())
        ),
        observed_effects=tuple(
            EffectKind(item) for item in (data.get("observed_effects") or ())
        ),
        raw_ref=str(data.get("raw_ref") or ""),
        error=str(data.get("error") or ""),
        started_at=float(data.get("started_at") or 0.0),
        ended_at=float(data.get("ended_at") or 0.0),
    )


def receipt_fact_ids(receipt: ToolReceipt) -> tuple[str, ...]:
    return tuple(fact_id(effect_to_payload(effect)) for effect in receipt.effects)


def receipt_event_payload(receipt: ToolReceipt) -> dict[str, Any]:
    return {
        "receipt": receipt_to_payload(receipt),
        "fact_ids": list(receipt_fact_ids(receipt)),
    }


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    task_id: str
    kind: EventKind
    source: EventSource
    payload: Mapping[str, Any]
    step_id: str | None = None
    ts: str = ""
    prev_hash: str = ""
    hash: str = ""
    mac: str = ""
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        *,
        seq: int,
        task_id: str,
        kind: EventKind,
        source: EventSource,
        payload: Mapping[str, Any],
        step_id: str | None = None,
        prev_hash: str = "",
        ts: str | None = None,
        hmac_key: bytes | None = None,
    ) -> "Event":
        if not isinstance(payload, Mapping):
            raise TypeError("event payload must be a mapping")
        timestamp = ts or utc_now()
        frozen_payload = copy.deepcopy(dict(payload))
        body = {
            "seq": seq,
            "task_id": task_id,
            "step_id": step_id,
            "kind": kind.value,
            "source": source.value,
            "ts": timestamp,
            "schema_version": SCHEMA_VERSION,
            "payload": frozen_payload,
            "prev_hash": prev_hash,
        }
        digest = _digest(body)
        mac = ""
        if hmac_key:
            mac = hmac_lib.new(
                hmac_key, digest.encode("utf-8"), hashlib.sha256
            ).hexdigest()
        return cls(
            seq=seq,
            task_id=task_id,
            kind=kind,
            source=source,
            payload=frozen_payload,
            step_id=step_id,
            ts=timestamp,
            prev_hash=prev_hash,
            hash=digest,
            mac=mac,
        )

    def _hash_body(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "kind": self.kind.value,
            "source": self.source.value,
            "ts": self.ts,
            "schema_version": self.schema_version,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        }

    def verify_hash(self) -> bool:
        return self.hash == _digest(self._hash_body())

    def verify_mac(self, hmac_key: bytes) -> bool:
        expected = hmac_lib.new(
            hmac_key, self.hash.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac_lib.compare_digest(self.mac, expected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "kind": self.kind.value,
            "source": self.source.value,
            "ts": self.ts,
            "schema_version": self.schema_version,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "mac": self.mac,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Event":
        payload = data.get("payload") or {}
        if not isinstance(payload, Mapping):
            raise ValueError("event payload must be a mapping")
        return cls(
            seq=int(data["seq"]),
            task_id=str(data["task_id"]),
            kind=EventKind(data["kind"]),
            source=EventSource(data["source"]),
            payload=payload,
            step_id=data.get("step_id"),
            ts=str(data.get("ts") or ""),
            prev_hash=str(data.get("prev_hash") or ""),
            hash=str(data.get("hash") or ""),
            mac=str(data.get("mac") or ""),
            schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
        )
