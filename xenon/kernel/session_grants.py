"""Session-level authorization: ask once, remember the human's decision (#373).

Xenon's product claim is **evidence verification**.  Permissions are *safety*,
not proof — yet the implementation asked on every single call, so in practice
people ran ``--permission bypass`` and the safety layer was bypassed entirely.
A safety measure that gets disabled is not a safety measure.

So the rule here mirrors Claude Code's fluency while keeping Xenon's semantics:

* the human answers once, and the answer is **recorded** (a ledger event), not a
  hidden setting;
* it is visible and revocable (``/permissions``);
* it **never** becomes evidence: no claim, no verdict, no ``VERIFIED``;
* only a human can create one — a model, or a child task, may only ask.

Scope is deliberately literal: a grant covers one capability within one
workspace.  ``always allow verify`` means "verify, in this repository", not
"verify anywhere".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from xenon.kernel.contracts import Capability, EffectKind
from xenon.kernel.policies import ScopedGrant

__all__ = [
    "ONCE",
    "SESSION",
    "SCOPED",
    "GrantBook",
    "grant_from_choice",
]

# The four answers an approval prompt may produce.
ONCE = "once"
SESSION = "session"
SCOPED = "scoped"
DENIED = "denied"

# A session grant has no TTL: it lives exactly as long as the session object
# that holds it.  Persisting it is an explicit user action (``/save``), and the
# session file is the only place it can come back from.
_NEVER = None


@dataclass(frozen=True, slots=True)
class SessionGrant:
    """One remembered human decision."""

    capability: Capability
    scope: str
    workspace: str
    added_at: float = field(default_factory=time.time)
    reason: str = ""

    def as_scoped(self) -> ScopedGrant:
        return ScopedGrant(
            capability=self.capability,
            target=self.scope,
            expires_at=_NEVER,
            effects=frozenset[EffectKind](),
            reason=self.reason,
        )

    def matches_workspace(self, workspace: str) -> bool:
        return _path(self.workspace) == _path(workspace)

    def payload(self) -> dict[str, Any]:
        return {
            "capability": self.capability.value,
            "scope": self.scope,
            "workspace": self.workspace,
            "added_at": self.added_at,
            "reason": self.reason,
        }

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> SessionGrant | None:
        """Parse a stored grant, or drop it rather than guess.

        A malformed grant that is silently kept would authorize *something*;
        dropping it only costs the user one prompt.
        """

        try:
            capability = Capability(str(data["capability"]))
        except (KeyError, ValueError):
            return None
        scope = str(data.get("scope") or "")
        workspace = str(data.get("workspace") or "")
        if not scope or not workspace:
            return None
        return cls(
            capability=capability,
            scope=scope,
            workspace=workspace,
            added_at=float(data.get("added_at") or time.time()),
            reason=str(data.get("reason") or ""),
        )


def _path(value: str) -> str:
    """Canonical form of a workspace path.

    Two spellings of the same directory must match, or a grant silently stops
    working.  A real probe hit exactly that: the front-end passed an MSYS
    ``/tmp/...`` path while the kernel recorded the resolved Windows one, so
    the stored grant never applied.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        from pathlib import Path

        text = str(Path(text).expanduser().resolve())
    except (OSError, RuntimeError):  # unresolvable path: compare literally
        pass
    return text.replace("\\", "/").rstrip("/").casefold()


class GrantBook:
    """The session's remembered decisions.

    Held by the front-end, not the kernel: the kernel receives plain
    ``ScopedGrant`` values and stays a pure function of its inputs.  That is what
    keeps a replayed ledger explainable — "why was this not asked?" is answered
    by the ``GRANT_ADDED`` event, not by hidden state.
    """

    def __init__(self, workspace: str = "") -> None:
        self.workspace = workspace
        self._grants: list[SessionGrant] = []

    def __len__(self) -> int:
        return len(self._grants)

    def __iter__(self) -> Iterable[SessionGrant]:
        return iter(tuple(self._grants))

    def add(self, grant: SessionGrant) -> SessionGrant:
        """Remember one decision, replacing an identical earlier one."""

        for index, existing in enumerate(self._grants):
            if (
                existing.capability is grant.capability
                and existing.scope == grant.scope
                and existing.matches_workspace(grant.workspace)
            ):
                self._grants[index] = grant
                return grant
        self._grants.append(grant)
        return grant

    def revoke(self, index: int) -> SessionGrant | None:
        """Drop the grant at ``index`` (1-based, as displayed)."""

        position = int(index) - 1
        if position < 0 or position >= len(self._grants):
            return None
        return self._grants.pop(position)

    def clear(self) -> None:
        self._grants.clear()

    def active(self, workspace: str | None = None) -> tuple[ScopedGrant, ...]:
        """Grants valid for this workspace, ready for the policy."""

        wanted = self.workspace if workspace is None else workspace
        return tuple(
            grant.as_scoped()
            for grant in self._grants
            if grant.matches_workspace(wanted)
        )

    def rows(self) -> list[dict[str, Any]]:
        return [grant.payload() for grant in self._grants]

    def to_payload(self) -> list[dict[str, Any]]:
        return self.rows()

    def load(self, rows: Any) -> None:
        """Restore from a session file, dropping anything unusable."""

        if not isinstance(rows, list):
            return
        restored: list[SessionGrant] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            grant = SessionGrant.from_payload(row)
            if grant is not None:
                restored.append(grant)
        self._grants = restored


def grant_from_choice(
    choice: str,
    *,
    capability: Capability,
    targets: Sequence[str],
    workspace: str,
    reason: str = "",
) -> SessionGrant | None:
    """Turn an approval answer into a grant, or ``None`` for "just this once".

    ``SESSION`` covers the whole capability in this workspace.  ``SCOPED`` covers
    exactly what was asked for — which is the same thing when a single target was
    requested, so a scoped answer with several targets is refused rather than
    silently widened to "all of them".

    The reason is *generated*, not borrowed: a grant's reason must say what the
    human allowed ("session grant: shell"), not why the policy asked
    ("verify requires approval").  A ledger replay answers "why was this not
    asked" with the former.
    """

    if choice == SESSION:
        return SessionGrant(
            capability=capability,
            scope="*",
            workspace=workspace,
            reason=f"session grant: {capability.value}",
        )
    if choice == SCOPED:
        if len(set(targets)) != 1:
            return None
        return SessionGrant(
            capability=capability,
            scope=str(targets[0]),
            workspace=workspace,
            reason=f"scoped grant: {targets[0]}",
        )
    return None
