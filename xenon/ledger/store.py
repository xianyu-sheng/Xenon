"""Append-only JSONL ledger with a hash chain and an anchor.

Design rules (see ``docs/EVENT_SOURCE.md`` and ADR 0001):

* one task, one file, one writer (``LoopRuntime`` owns the only writable handle);
* a receipt is an atomic event, not a prose log line;
* every event carries ``seq``, ``prev_hash`` and a content hash;
* an anchor records a checkpoint so a truncated tail can be detected;
* HMAC is optional and never silently waived.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from xenon.kernel.contracts import ToolReceipt
from xenon.ledger.events import (
    Event,
    EventKind,
    EventSource,
    canonical_json,
    receipt_event_payload,
    utc_now,
)

ANCHOR_SUFFIX = ".anchor"


class LedgerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    valid: bool
    reason: str = ""
    events: int = 0
    last_seq: int = 0
    last_hash: str = ""
    truncated_tail: bool = False
    keyed: bool = False
    key_required: bool = False
    anchor_seq: int = 0
    anchor_hash: str = ""


class Ledger:
    """One append-only ledger file for one task."""

    def __init__(
        self,
        path: Path | str | None,
        task_id: str,
        hmac_key: bytes | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.task_id = task_id
        self._hmac_key = hmac_key
        self._events: list[Event] = []
        self._load_error = ""
        self._truncated_tail = False
        if self.path is not None and self.path.exists():
            self._load()

    @classmethod
    def open(
        cls,
        path: Path | str,
        task_id: str = "",
        hmac_key: bytes | None = None,
    ) -> "Ledger":
        ledger = cls(path, task_id, hmac_key)
        if not ledger.task_id and ledger._events:
            ledger.task_id = ledger._events[0].task_id
        return ledger

    @classmethod
    def in_memory(
        cls, task_id: str, hmac_key: bytes | None = None
    ) -> "Ledger":
        return cls(None, task_id, hmac_key)

    # ── write path ─────────────────────────────────────────────────────

    def append(
        self,
        kind: EventKind,
        source: EventSource,
        payload: Mapping[str, Any],
        *,
        step_id: str | None = None,
        ts: str | None = None,
    ) -> Event:
        if self._load_error:
            raise LedgerError(f"ledger is corrupt: {self._load_error}")
        if self._truncated_tail:
            raise LedgerError("ledger has an incomplete tail; recover first")
        if not self.task_id:
            raise LedgerError("task_id is required before appending")
        seq = len(self._events) + 1
        prev_hash = self._events[-1].hash if self._events else ""
        event = Event.create(
            seq=seq,
            task_id=self.task_id,
            kind=kind,
            source=source,
            payload=payload,
            step_id=step_id,
            prev_hash=prev_hash,
            ts=ts,
            hmac_key=self._hmac_key,
        )
        if self.path is not None:
            self._write_event(event)
            self._write_anchor_best_effort(event)
        self._events.append(event)
        return event

    def append_receipt(
        self, receipt: ToolReceipt, *, step_id: str | None = None
    ) -> Event:
        return self.append(
            EventKind.TOOL_RECEIPT,
            EventSource.TOOL,
            receipt_event_payload(receipt),
            step_id=step_id or receipt.step_id,
        )

    def append_event(self, event: Event) -> Event:
        """Append an externally built event (replay/import).  Validates the chain."""

        if self._load_error:
            raise LedgerError(f"ledger is corrupt: {self._load_error}")
        if self._truncated_tail:
            raise LedgerError("ledger has an incomplete tail; recover first")
        if event.seq != len(self._events) + 1:
            raise LedgerError(
                f"expected seq {len(self._events) + 1}, got {event.seq}"
            )
        prev = self._events[-1].hash if self._events else ""
        if event.prev_hash != prev:
            raise LedgerError("event prev_hash does not match the ledger tail")
        if not event.verify_hash():
            raise LedgerError("event hash does not verify")
        if event.task_id != self.task_id:
            raise LedgerError("event task_id does not match the ledger")
        if self.path is not None:
            self._write_event(event)
            self._write_anchor_best_effort(event)
        self._events.append(event)
        return event

    # ── read path ──────────────────────────────────────────────────────

    def read(
        self,
        kind: EventKind | str | None = None,
        up_to_seq: int | None = None,
    ) -> tuple[Event, ...]:
        expected = kind.value if isinstance(kind, EventKind) else kind
        limit = up_to_seq if up_to_seq is not None else len(self._events)
        selected: list[Event] = []
        for event in self._events:
            if event.seq > limit:
                break
            if expected is None or event.kind.value == expected:
                selected.append(event)
        return tuple(selected)

    def watermark(self) -> int:
        return self._events[-1].seq if self._events else 0

    def last_hash(self) -> str:
        return self._events[-1].hash if self._events else ""

    @property
    def truncated_tail(self) -> bool:
        return self._truncated_tail

    @property
    def load_error(self) -> str:
        return self._load_error

    # ── integrity ──────────────────────────────────────────────────────

    def verify(self, hmac_key: bytes | None = None) -> IntegrityReport:
        if self._load_error:
            return IntegrityReport(
                valid=False,
                reason=self._load_error,
                events=len(self._events),
                last_seq=self.watermark(),
                last_hash=self.last_hash(),
            )
        if self._truncated_tail:
            return IntegrityReport(
                valid=False,
                reason="incomplete tail",
                events=len(self._events),
                last_seq=self.watermark(),
                last_hash=self.last_hash(),
                truncated_tail=True,
            )

        key = hmac_key if hmac_key is not None else self._hmac_key
        keyed = any(event.mac for event in self._events)
        previous_hash = ""
        for index, event in enumerate(self._events, start=1):
            if self.task_id and event.task_id != self.task_id:
                return self._invalid(
                    f"task_id mismatch at seq {event.seq}",
                    keyed=keyed,
                    key_required=bool(keyed and not key),
                )
            if event.seq != index:
                return self._invalid(
                    f"seq gap at {index}: found {event.seq}",
                    keyed=keyed,
                    key_required=bool(keyed and not key),
                )
            if event.prev_hash != previous_hash:
                return self._invalid(
                    f"prev_hash mismatch at seq {event.seq}",
                    keyed=keyed,
                    key_required=bool(keyed and not key),
                )
            if not event.verify_hash():
                return self._invalid(
                    f"hash mismatch at seq {event.seq}",
                    keyed=keyed,
                    key_required=bool(keyed and not key),
                )
            if keyed:
                if not key:
                    return self._invalid(
                        "hmac key required",
                        keyed=True,
                        key_required=True,
                    )
                if not event.mac or not event.verify_mac(key):
                    return self._invalid(
                        f"hmac mismatch at seq {event.seq}",
                        keyed=True,
                    )
            previous_hash = event.hash

        anchor = self._read_anchor()
        if anchor is not None:
            anchor_seq = int(anchor.get("seq") or 0)
            anchor_hash = str(anchor.get("hash") or "")
            if anchor_seq < 0 or anchor_hash == "__broken__":
                return self._invalid(
                    "anchor is unreadable",
                    keyed=keyed,
                    anchor_seq=anchor_seq,
                    anchor_hash=anchor_hash,
                )
            if anchor_seq > len(self._events):
                return self._invalid(
                    "ledger is shorter than its anchor (truncated?)",
                    keyed=keyed,
                    anchor_seq=anchor_seq,
                    anchor_hash=anchor_hash,
                )
            if anchor_seq > 0:
                anchored = self._events[anchor_seq - 1]
                if anchored.hash != anchor_hash:
                    return self._invalid(
                        "anchor hash mismatch",
                        keyed=keyed,
                        anchor_seq=anchor_seq,
                        anchor_hash=anchor_hash,
                    )
            if anchor.get("task_id") not in (None, "", self.task_id):
                return self._invalid(
                    "anchor task_id mismatch",
                    keyed=keyed,
                    anchor_seq=anchor_seq,
                    anchor_hash=anchor_hash,
                )

        return IntegrityReport(
            valid=True,
            events=len(self._events),
            last_seq=self.watermark(),
            last_hash=self.last_hash(),
            keyed=keyed,
            anchor_seq=int((anchor or {}).get("seq") or 0),
            anchor_hash=str((anchor or {}).get("hash") or ""),
        )

    def _invalid(self, reason: str, **extra: Any) -> IntegrityReport:
        return IntegrityReport(
            valid=False,
            reason=reason,
            events=len(self._events),
            last_seq=self.watermark(),
            last_hash=self.last_hash(),
            **extra,
        )

    # ── projections ────────────────────────────────────────────────────

    def project(
        self, name: str, up_to_seq: int | None = None
    ) -> dict[str, Any]:
        from xenon.ledger.projections import project

        return project(name, self.read(up_to_seq=up_to_seq))

    def projection_hash(self, name: str, up_to_seq: int | None = None) -> str:
        import hashlib

        payload = canonical_json(self.project(name, up_to_seq=up_to_seq))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ── recovery ───────────────────────────────────────────────────────

    def recover_truncated(self) -> Path | None:
        """Move an incomplete tail aside and reopen the last complete prefix."""

        if self.path is None or not self._truncated_tail:
            return None
        raw = self.path.read_bytes()
        last_newline = raw.rfind(b"\n")
        tail = raw[last_newline + 1 :]
        if not tail:
            self._truncated_tail = False
            return None
        stamp = utc_now().replace(":", "").replace(".", "")
        corrupt = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        corrupt.write_bytes(tail)
        self.path.write_bytes(raw[: last_newline + 1])
        self._events = []
        self._load_error = ""
        self._truncated_tail = False
        self._load()
        return corrupt

    # ── persistence ────────────────────────────────────────────────────

    def _load(self) -> None:
        assert self.path is not None
        raw = self.path.read_bytes()
        if not raw:
            return
        lines = raw.split(b"\n")
        complete = raw.endswith(b"\n")
        for index, line in enumerate(lines, start=1):
            if not line:
                if index != len(lines):
                    self._load_error = f"line {index}: blank line"
                    break
                continue
            is_last = index == len(lines)
            if is_last and not complete:
                self._truncated_tail = True
                break
            try:
                data = json.loads(line.decode("utf-8"))
                event = Event.from_dict(data)
            except Exception as exc:  # noqa: BLE001 — report, never hide
                self._load_error = f"line {index}: {exc}"
                break
            self._events.append(event)

    def _write_event(self, event: Event) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(event.to_dict()) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _write_anchor_best_effort(self, event: Event) -> None:
        """An anchor failure must not lose a durable event; verify can catch up."""

        try:
            self._write_anchor(event)
        except OSError:
            return

    def _write_anchor(self, event: Event) -> None:
        assert self.path is not None
        anchor = {
            "task_id": self.task_id,
            "seq": event.seq,
            "hash": event.hash,
            "updated_at": utc_now(),
        }
        target = self._anchor_path()
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(canonical_json(anchor), encoding="utf-8")
        os.replace(temp, target)

    def _anchor_path(self) -> Path:
        assert self.path is not None
        return self.path.with_name(self.path.name + ANCHOR_SUFFIX)

    def _read_anchor(self) -> Mapping[str, Any] | None:
        if self.path is None:
            return None
        anchor = self._anchor_path()
        if not anchor.exists():
            return None
        try:
            return json.loads(anchor.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a broken anchor is invalid, not absent
            return {"seq": -1, "hash": "__broken__"}


class ReadOnlyLedger:
    """Read-only view for projections.  Writing is a type error at runtime."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def read(
        self,
        kind: EventKind | str | None = None,
        up_to_seq: int | None = None,
    ) -> tuple[Event, ...]:
        return self._ledger.read(kind=kind, up_to_seq=up_to_seq)

    def watermark(self) -> int:
        return self._ledger.watermark()

    def verify(self, hmac_key: bytes | None = None) -> IntegrityReport:
        return self._ledger.verify(hmac_key=hmac_key)

    def project(
        self, name: str, up_to_seq: int | None = None
    ) -> dict[str, Any]:
        return self._ledger.project(name, up_to_seq=up_to_seq)

    def append(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("read-only ledger view cannot append")


def read_events(path: Path | str) -> Sequence[Event]:
    return Ledger.open(path).read()
