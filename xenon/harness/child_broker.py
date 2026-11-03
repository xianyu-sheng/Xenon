"""Brokered approval for child runtimes (#356).

A child process has no terminal, so it cannot ask the human anything.  Without a
broker it is therefore structurally unable to run a test or a command: the
policy says "needs a grant", and there is nobody to grant it.  Observed on a real
run, a child that had already edited a file ended ``blocked`` on
``verify requires a grant``.

The broker does **not** create authority.  It forwards each request to the
parent's own approval provider — under a TTY that is still the human pressing a
key.  A child can therefore never obtain more than its parent already could, and
if the parent cannot answer, the request is denied (fail closed).

Transport is two files per request, because a file cannot be mistaken for a
result and needs no protocol version:

    <dir>/req-<n>.json   child  -> parent   {id, capability, tool, params, risk}
    <dir>/resp-<n>.json  parent -> child    {approved, reason}

Why not a pipe: the child's stdout is ``DEVNULL`` by design (a child's output is
not its result), and adding a second channel with framing rules is exactly the
kind of protocol that drifts.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REQUEST_PREFIX = "req-"
RESPONSE_PREFIX = "resp-"
# How long a child waits for the parent before giving up.  Short on purpose: a
# stalled approval should surface as a blocked child, not as a hung session.
DEFAULT_WAIT_SECONDS = 600.0
POLL_SECONDS = 0.15


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


class BrokerApprovalProvider:
    """Child side: publish a request, wait for the parent's answer."""

    def __init__(self, directory: str | Path, *, wait_seconds: float | None = None) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.wait_seconds = (
            DEFAULT_WAIT_SECONDS if wait_seconds is None else float(wait_seconds)
        )
        self.last_reason = ""
        self._counter = 0

    def request(self, view: Any, action: Any, decision: Any) -> bool:
        del view, decision
        intent = getattr(action, "intent", None)
        capability = getattr(intent, "capability", None)
        if capability is None:
            self.last_reason = "approval only covers tool calls"
            return False
        self._counter += 1
        request_id = f"{os.getpid()}-{self._counter}"
        _write_atomic(
            self.directory / f"{REQUEST_PREFIX}{request_id}.json",
            {
                "id": request_id,
                "capability": getattr(capability, "value", str(capability)),
                "tool": getattr(capability, "value", str(capability)),
                "params": dict(getattr(intent, "params", {}) or {}),
                "risk": "approval",
            },
        )
        answer = self._await(request_id)
        if answer is None:
            # #371: a timeout is a deny with a readable ledger reason, never a
            # hang.  The child's kernel writes this into USER_APPROVAL.
            self.last_reason = f"approval timed out after {self.wait_seconds:.0f}s"
            return False
        approved = bool(answer.get("approved"))
        if not approved:
            self.last_reason = str(answer.get("reason") or "parent denied the request")
        return approved

    def _await(self, request_id: str) -> dict[str, Any] | None:
        deadline = time.time() + self.wait_seconds
        response = self.directory / f"{RESPONSE_PREFIX}{request_id}.json"
        while time.time() < deadline:
            payload = _read_json(response) if response.exists() else None
            if payload is not None:
                return payload
            time.sleep(POLL_SECONDS)
        return None


@dataclass(frozen=True, slots=True)
class ServedRequest:
    request_id: str
    answered: bool
    approved: bool
    reason: str


def serve_requests(
    directory: str | Path,
    provider: Any,
    *,
    seen: set[str] | None = None,
) -> list[ServedRequest]:
    """Parent side: answer every pending request once.

    Runs on the parent's own thread while it waits for the child, so the TTY
    prompt happens where the REPL normally prompts — no second thread touching
    the terminal.  ``seen`` lets a caller that services repeatedly avoid asking
    twice for the same request.
    """

    root = Path(directory)
    if not root.is_dir():
        return []
    handled = seen if seen is not None else set()
    served: list[ServedRequest] = []
    for path in sorted(root.glob(f"{REQUEST_PREFIX}*.json")):
        request = _read_json(path)
        if request is None:
            continue
        request_id = str(request.get("id") or path.stem)
        if request_id in handled:
            continue
        handled.add(request_id)
        approved, reason = _ask(provider, request)
        _write_atomic(
            root / f"{RESPONSE_PREFIX}{request_id}.json",
            {"approved": approved, "reason": reason},
        )
        served.append(
            ServedRequest(
                request_id=request_id,
                answered=True,
                approved=approved,
                reason=reason,
            )
        )
    return served


def _ask(provider: Any, request: dict[str, Any]) -> tuple[bool, str]:
    """Ask the parent's provider.  No provider means no authority, not a yes."""

    if provider is None:
        return False, "no approval provider on the parent"
    ask: Callable[..., Any] | None = getattr(provider, "approve_request", None)
    if ask is None:
        return False, "parent approval provider cannot answer file requests"
    try:
        answer = ask(request)
    except Exception as exc:  # noqa: BLE001 — a broken UI must deny, not allow
        return False, f"parent approval failed: {exc}"
    if isinstance(answer, tuple):
        return bool(answer[0]), str(answer[1] or "")
    return bool(answer), ""


__all__ = [
    "BrokerApprovalProvider",
    "DEFAULT_WAIT_SECONDS",
    "POLL_SECONDS",
    "REQUEST_PREFIX",
    "RESPONSE_PREFIX",
    "ServedRequest",
    "serve_requests",
]
