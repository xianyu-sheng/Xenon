"""Session authorization: the three answers design doc §9 defines.

Xenon's product claim is **evidence verification**.  Permissions are *safety*,
not proof — yet the implementation asked on every single call, so in practice
people ran ``--permission bypass`` and the safety layer was bypassed entirely.
A safety measure that gets disabled is not a safety measure.

§9 defines exactly three answers:

===============  =========================================================
``once``         allow this call; the next one asks again
``allow``        this **tool + this execution path** stops asking, for a
                 limited time (30 minutes)
``deny``         refuse this call
===============  =========================================================

Consequences that are load-bearing, not incidental:

* **no capability-wide answer.**  §9 says "工具 + 执行路径", so "always allow"
  covers the command or path the human saw — never the whole ``shell``
  capability.  The older ``SESSION`` wildcard meant one ``a`` press released
  every later shell command, including destructive ones.
* **memory only, never disk** (§9 and decision 13.2#8).  There is deliberately
  no serialization here: a "session" answer must not survive a restart.
* **it never becomes evidence**: no claim, no verdict, no ``VERIFIED``.
* **only a human creates one** — a model, or a child task, may only ask.

This module is the *only* place an authorization is remembered.  The REPL holds
one ``GrantBook`` and hands the kernel plain ``ScopedGrant`` values, so the
kernel stays a pure function of its inputs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from xenon.kernel.contracts import Capability, EffectKind
from xenon.kernel.policies import ScopedGrant

__all__ = [
    "ALLOW",
    "DEFAULT_GRANT_TTL_SECONDS",
    "DENIED",
    "ONCE",
    "TURN_ALLOW",
    "GrantBook",
    "grant_from_choice",
]

# The three answers of §9, plus the one extra answer the panel offers inside a
# turn: "don't ask me again for this capability until this turn ends".  It is
# deliberately NOT a session-wide answer and NOT a rule file — see
# ``GrantBook.remember_turn``.
ONCE = "once"
ALLOW = "allow"
TURN_ALLOW = "turn"
DENIED = "denied"

# "…给予时间期限（如 30 分钟）" from §9.
DEFAULT_GRANT_TTL_SECONDS = 1800.0


@dataclass(frozen=True, slots=True)
class SessionGrant:
    """One remembered decision: this capability, on this target, until N.

    ``targets`` is the list of concrete execution paths the human saw (a
    command string, a file path, a URL).  It is never ``*``: a grant that
    covers "anything" is not something §9 offers.

    ``turn`` records which turn the answer was given in.  It is what makes
    "同一轮内不再重复问同一个能力" possible: a later call in the *same* turn can
    reuse the answer, while the next turn asks again.  A grant from an earlier
    turn carries its own turn id and therefore does not cover a new one.
    """

    capability: Capability
    targets: tuple[str, ...]
    workspace: str
    added_at: float = field(default_factory=time.time)
    reason: str = ""
    expires_at: float | None = None
    # ``True`` for an ``once`` answer: applies to the next matching call and is
    # removed when it answers one.
    one_shot: bool = False
    # Which turn this answer was given in.  ``""`` means "not turn-bound".
    turn: str = ""

    def active(self, now: float | None = None) -> bool:
        """Whether this grant can still answer a call.

        A one-shot answer has no deadline — it lives until the call it allows
        consumes it (``GrantBook.consume``).  A timed answer dies at its
        deadline.
        """

        if self.one_shot:
            return True
        if self.expires_at is None:
            return True
        return (time.time() if now is None else now) < self.expires_at

    def as_scoped(self) -> tuple[ScopedGrant, ...]:
        """One grant per target, because the policy matches a target at a time."""

        return tuple(
            ScopedGrant(
                capability=self.capability,
                target=target,
                expires_at=self.expires_at,
                effects=frozenset[EffectKind](),
                reason=self.reason,
            )
            for target in self.targets
        )

    def matches_workspace(self, workspace: str) -> bool:
        return _path(self.workspace) == _path(workspace)


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
    """The session's remembered decisions — the only such store.

    Held by the front-end, not the kernel: the kernel receives plain
    ``ScopedGrant`` values and stays a pure function of its inputs.

    Memory only (see the module docstring).  A one-shot answer is consumed by
    the call it allows; a timed answer is pruned lazily on every read, so the
    count a listing shows is the count in force.
    """

    def __init__(
        self, workspace: str = "", *, ttl_seconds: float | None = None
    ) -> None:
        self.workspace = workspace
        self.ttl_seconds = (
            DEFAULT_GRANT_TTL_SECONDS if ttl_seconds is None else float(ttl_seconds)
        )
        self._grants: list[SessionGrant] = []

    def _prune(self, now: float | None = None) -> None:
        """Drop grants that can no longer answer a call."""

        if not self._grants:
            return
        moment = time.time() if now is None else now
        self._grants = [g for g in self._grants if g.active(moment)]

    def __len__(self) -> int:
        self._prune()
        return len(self._grants)

    def __iter__(self) -> Iterable[SessionGrant]:
        self._prune()
        return iter(tuple(self._grants))

    def add(self, grant: SessionGrant) -> SessionGrant:
        """Remember one decision, replacing an identical earlier one.

        A timed grant without a deadline inherits the book's, so "how long does
        an answer last" has one owner.  A one-shot grant never gets one.
        """

        if not grant.one_shot and grant.expires_at is None and self.ttl_seconds > 0:
            grant = SessionGrant(
                capability=grant.capability,
                targets=grant.targets,
                workspace=grant.workspace,
                added_at=grant.added_at,
                reason=grant.reason,
                expires_at=grant.added_at + self.ttl_seconds,
                turn=grant.turn,
            )
        self._prune()
        for index, existing in enumerate(self._grants):
            if (
                existing.capability is grant.capability
                and existing.targets == grant.targets
                and existing.one_shot == grant.one_shot
                and existing.turn == grant.turn
                and existing.matches_workspace(grant.workspace)
            ):
                self._grants[index] = grant
                return grant
        self._grants.append(grant)
        return grant

    def remember_turn(self, turn: str, capability: Capability) -> SessionGrant | None:
        """Record a "不再问" answer that covers this turn's whole capability.

        A real task pressed ``a`` for one command and the model then asked to
        run four different spellings of the same intent — quoting, a stdin
        pipe, the program's full path.  Each was a genuinely different path, so
        each asked again, and the operator saw four panels for one intention.

        This is the narrow fix: the answer is bound to **this turn only**.  It
        does not survive into the next turn, it does not reach another
        capability, and hard safety rules (destructive / push) are still
        evaluated before any grant, so it cannot release what the policy
        refuses.

        Returns ``None`` when the caller supplied no turn id — without that
        bound the answer would silently become session-wide.
        """

        if not turn:
            return None
        return self.add(
            SessionGrant(
                capability=capability,
                targets=("*",),
                workspace=self.workspace,
                reason=f"this turn: {capability.value}",
                turn=turn,
            )
        )

    def revoke(self, index: int) -> SessionGrant | None:
        """Drop the grant at ``index`` (1-based, as displayed)."""

        self._prune()
        position = int(index) - 1
        if position < 0 or position >= len(self._grants):
            return None
        return self._grants.pop(position)

    def clear(self) -> None:
        self._grants.clear()

    def active(
        self,
        workspace: str | None = None,
        capability: Capability | None = None,
        targets: Sequence[str] | None = None,
    ) -> tuple[ScopedGrant, ...]:
        """Grants in force for this workspace, ready for the policy.

        ``capability``/``targets`` narrow the result to what a specific call
        needs; leaving them out lists everything in force (what ``/permissions``
        shows).
        """

        self._prune()
        wanted = self.workspace if workspace is None else workspace
        chosen: list[ScopedGrant] = []
        for grant in self._grants:
            if grant.turn:
                # A turn-scoped answer belongs to that one turn; a caller that
                # lists grants (e.g. a sub-agent inherits ``active()``) must
                # not receive authority that was scoped to the parent's turn.
                continue
            if not grant.matches_workspace(wanted):
                continue
            if capability is not None and grant.capability is not capability:
                continue
            if targets is not None and not _targets_covered(grant, targets):
                continue
            chosen.extend(grant.as_scoped())
        return tuple(chosen)

    def consume(
        self,
        workspace: str | None = None,
        capability: Capability | None = None,
        targets: Sequence[str] | None = None,
        turn: str | None = None,
    ) -> tuple[ScopedGrant, ...]:
        """Like ``active``, but spends what can only answer once.

        Two things are spent here:

        * a one-shot ``once`` answer, so "本次允许" really means once;
        * a turn-scoped answer whose turn is not the current one, so
          "同一轮内不再问" never leaks into the next turn.

        ``turn=None`` means "answer for no particular turn", which drops every
        turn-scoped grant: a caller that cannot name a turn must not inherit
        authority that was granted to a different one.
        """

        self._prune()
        wanted = self.workspace if workspace is None else workspace
        chosen: list[ScopedGrant] = []
        spent: list[int] = []
        for index, grant in enumerate(self._grants):
            if grant.turn and grant.turn != turn:
                spent.append(index)
                continue
            if not grant.matches_workspace(wanted):
                continue
            if capability is not None and grant.capability is not capability:
                continue
            if targets is not None and not _targets_covered(grant, targets):
                continue
            chosen.extend(grant.as_scoped())
            if grant.one_shot:
                spent.append(index)
        for index in reversed(spent):
            self._grants.pop(index)
        return tuple(chosen)


def _targets_covered(grant: SessionGrant, wanted: Sequence[str]) -> bool:
    """Every target the call touches is covered by this grant.

    Delegates to ``ScopedGrant.allows`` instead of repeating its rules.  There
    used to be a second, literal comparison here, and the two disagreed: the
    policy accepted ``python "-c" …`` for a grant on ``python -c "…"`` while the
    book rejected it first, so the *stricter* copy silently won and the human
    was asked again.  One matcher, one meaning.
    """

    if not wanted:
        return False
    scoped = grant.as_scoped()
    return all(
        any(item.allows(grant.capability, target) for item in scoped)
        for target in wanted
    )


def grant_from_choice(
    choice: str,
    *,
    capability: Capability,
    targets: Sequence[str],
    workspace: str,
    reason: str = "",
    turn: str = "",
    ttl_seconds: float = DEFAULT_GRANT_TTL_SECONDS,
) -> SessionGrant | None:
    """Turn a §9 answer into a grant, or ``None`` when nothing is remembered.

    ``once`` produces a single-use grant (remembered only for the call in
    flight).  ``allow`` produces a timed grant for exactly the targets shown.
    ``denied`` and anything unrecognised produce nothing — a refusal is not a
    grant, and an unknown answer must fail closed rather than guess.

    A call with no concrete target cannot be scoped, so it produces nothing:
    remembering "some shell command" would authorize more than the human saw.

    ``turn`` binds the answer to one turn so the same capability is not asked
    about again inside it (``GrantBook.remember_turn`` covers the whole
    capability; this covers the exact paths shown).
    """

    concrete = tuple(str(t) for t in targets if str(t).strip())
    if not concrete:
        return None
    if choice == ONCE:
        return SessionGrant(
            capability=capability,
            targets=concrete,
            workspace=workspace,
            reason=f"once: {concrete[0]}",
            one_shot=True,
            turn=turn,
        )
    if choice == ALLOW:
        # "全部允许" is a *timed session* answer: it must survive the turn it
        # was given in (design doc §9: 30-minute window), so it carries no
        # turn stamp.  Stamping it made the very next turn's consume() drop
        # it while the panel had just promised "本会话内不再询问".
        return SessionGrant(
            capability=capability,
            targets=concrete,
            workspace=workspace,
            reason=f"session grant: {concrete[0]}",
            # The deadline belongs on the grant itself: callers push it into
            # the running policy as well as into the book, and an unstamped
            # copy let a long run keep the authority past §9's window.
            expires_at=(time.time() + ttl_seconds) if ttl_seconds > 0 else None,
        )
    if choice == TURN_ALLOW:
        # Whole capability, but only until this turn ends.  ``consume`` drops
        # it as soon as a different turn asks.
        if not turn:
            return None
        return SessionGrant(
            capability=capability,
            targets=concrete,
            workspace=workspace,
            reason=f"this turn: {capability.value}",
            turn=turn,
        )
    return None
