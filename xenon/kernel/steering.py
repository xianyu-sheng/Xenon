"""Operator steering as ledger events.

Steering is a *fact* the operator adds, never a mutation of what already
happened.  Concretely:

- a constraint appends; it never removes an obligation or an earlier
  constraint (deleting an obligation requires ``user_accepted_debt``);
- a correction marks an earlier event as superseded **in the view**; the
  event itself stays exactly as written, because the ledger is append-only;
- nothing here touches permissions: approval is the only path to more
  authority.

The module is pure: parsing and bookkeeping only.  The runtime decides when
to read it (at action boundaries) and the view decides how to render it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

__all__ = [
    "SteeringError",
    "SteeringState",
    "parse_steering",
    "KIND_CONSTRAINT",
    "KIND_STRATEGY",
    "KIND_CORRECTION",
    "SCOPE_TASK",
    "SCOPE_TURN",
]

KIND_CONSTRAINT = "constraint"
KIND_STRATEGY = "strategy"
KIND_CORRECTION = "correction"
_KINDS = frozenset({KIND_CONSTRAINT, KIND_STRATEGY, KIND_CORRECTION})

SCOPE_TASK = "task"
SCOPE_TURN = "turn"
_SCOPES = frozenset({SCOPE_TASK, SCOPE_TURN})

_MAX_TEXT = 2000


class SteeringError(ValueError):
    """An operator input the loop cannot act on."""


def parse_steering(
    text: str, *, kind: str = KIND_CONSTRAINT, scope: str = SCOPE_TASK,
    target_seq: int = 0,
) -> dict[str, Any]:
    """Validate one operator input into a ledger-ready payload.

    Fails closed: an unknown kind, an empty body or a nonsense scope raises
    instead of being silently reinterpreted.
    """

    body = str(text or "").strip()
    if not body:
        raise SteeringError("steering text is required")
    if len(body) > _MAX_TEXT:
        raise SteeringError(f"steering text is longer than {_MAX_TEXT} chars")
    if kind not in _KINDS:
        raise SteeringError(f"unknown steering kind: {kind!r}")
    if scope not in _SCOPES:
        raise SteeringError(f"unknown steering scope: {scope!r}")
    payload: dict[str, Any] = {"kind": kind, "scope": scope, "text": body}
    if kind == KIND_CORRECTION:
        try:
            seq = int(target_seq)
        except (TypeError, ValueError) as exc:
            raise SteeringError("correction needs a numeric target_seq") from exc
        if seq <= 0:
            raise SteeringError("correction needs a positive target_seq")
        payload["target_seq"] = seq
    elif target_seq:
        # A constraint or strategy hint is never retroactive.
        raise SteeringError("only corrections may target a past event")
    return payload


@dataclass
class SteeringState:
    """What the loop has been told so far, in event order."""

    constraints: list[tuple[int, str, str]] = field(default_factory=list)
    strategy_hints: list[tuple[int, str]] = field(default_factory=list)
    corrections: dict[int, tuple[int, str]] = field(default_factory=dict)
    consumed_up_to: int = 0

    def apply(self, seq: int, payload: Mapping[str, Any]) -> None:
        kind = str(payload.get("kind") or KIND_CONSTRAINT)
        text = str(payload.get("text") or "")
        if not text:
            return
        if kind == KIND_CONSTRAINT:
            scope = str(payload.get("scope") or SCOPE_TASK)
            self.constraints.append((int(seq), text, scope))
        elif kind == KIND_STRATEGY:
            self.strategy_hints.append((int(seq), text))
        elif kind == KIND_CORRECTION:
            target = int(payload.get("target_seq") or 0)
            if target > 0:
                self.corrections[target] = (int(seq), text)
        self.consumed_up_to = max(self.consumed_up_to, int(seq))

    def consume(self, events: Iterable[Any]) -> int:
        """Apply every unseen ``USER_INPUT`` event; returns how many were new."""

        applied = 0
        for event in events:
            seq = int(getattr(event, "seq", 0) or 0)
            if seq <= self.consumed_up_to:
                continue
            kind = getattr(getattr(event, "kind", None), "value", "")
            if kind != "user_input":
                continue
            self.apply(seq, dict(getattr(event, "payload", {}) or {}))
            applied += 1
        return applied

    # ── what the view needs ────────────────────────────────────────────

    def extra_constraints(self) -> tuple[tuple[int, str], ...]:
        return tuple((seq, text) for seq, text, _scope in self.constraints)

    def superseded(self) -> frozenset[int]:
        return frozenset(self.corrections)

    def superseded_reason(self, seq: int) -> str:
        item = self.corrections.get(int(seq))
        return item[1] if item else ""

    def strategy_hint(self) -> str:
        return self.strategy_hints[-1][1] if self.strategy_hints else ""

    def reset(self) -> None:
        self.constraints.clear()
        self.strategy_hints.clear()
        self.corrections.clear()
        self.consumed_up_to = 0
