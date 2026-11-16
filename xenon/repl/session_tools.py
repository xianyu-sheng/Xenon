"""Session-stable MCP tool contract (append-only, schema-stable).

A provider's cache is an exact byte prefix, and the wire order is
``system -> tools -> messages``.  Selecting MCP tools per *task* therefore
invalidates the tool block at every task boundary, and expanding a tool's
schema after it runs invalidates it mid-task.  Both were measured on the live
API: a new task's first call hit only the system prompt plus the first slice of
the tool block.

The contract here makes the tool block a function of the **session**, not of
the task:

* the selected set is a monotone union in first-seen order -- a later task can
  append tools, never reorder or remove the earlier ones;
* a tool keeps the schema it entered the session with; execution may expand it
  in the current task (the kernel still does that), and the REPL records the
  expansion so every later task in the same session sends the expanded form;
* the set is capped (least-recently-selected evicted first, never a tool the
  current task just selected), so a long session cannot grow the prompt
  without bound.  Eviction is a one-time tail miss, not a per-task one.

A tool that disappears from the registry or is excluded by an explicit
allowlist is pruned; the allowlist is an operator decision and outranks cache
stability.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from xenon.catalog.specs import select_mcp_tool_specs
from xenon.kernel.contracts import ToolSpec

__all__ = [
    "DEFAULT_SESSION_TOOL_CAP",
    "SessionToolContract",
]

#: A selection is one task's relevance budget (default 16); the session keeps
#: at most this many so later tasks rarely evict.
DEFAULT_SESSION_TOOL_CAP = 32


@dataclass
class SessionToolContract:
    """The MCP tools one session exposes, in stable first-seen order."""

    session_id: str = ""
    cap: int = DEFAULT_SESSION_TOOL_CAP
    _order: list[str] = field(default_factory=list)
    _specs: dict[str, ToolSpec] = field(default_factory=dict)
    _last_task: dict[str, int] = field(default_factory=dict)
    _task: int = 0
    _last_added: tuple[str, ...] = ()
    #: Tools whose full schema the model has already seen (they ran in an
    #: earlier task).  Session-stable so the next task's tool block matches the
    #: previous task's last request instead of resetting to index rows.
    expanded: set[str] = field(default_factory=set)

    @property
    def last_added(self) -> tuple[str, ...]:
        """Names this session's contract gained in the most recent selection."""

        return self._last_added

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._order)

    def select(
        self,
        specs: tuple[ToolSpec, ...] | list[ToolSpec],
        objective: str,
        *,
        limit: int,
        allowed: tuple[str, ...] | list[str] = (),
    ) -> tuple[ToolSpec, ...]:
        """Extend the contract with this task's selection and return it.

        The returned tuple is the *whole* session contract (earlier tasks'
        tools included), in first-seen order.  ``last_added`` names only what
        this task introduced, which is what the ledger records.
        """

        self._task += 1
        available = {spec.name: spec for spec in specs}
        allow = tuple(str(item).strip() for item in allowed if str(item).strip())
        allow_set = set(allow)

        # Prune what the registry no longer has, and what an explicit operator
        # allowlist excludes.  Cache stability yields to correctness.
        for name in tuple(self._order):
            if name not in available or (allow_set and name not in allow_set):
                self._forget(name)

        chosen = select_mcp_tool_specs(
            tuple(available.values()),
            objective,
            limit=limit,
            allowed=allow,
            # A session contract is append-only, so a weakly-related tool is
            # paid for by every later task.  The per-task prompt keeps the
            # canonical budget fill; the contract grows only on evidence.
            relevant_only=True,
        )
        added: list[str] = []
        for spec in chosen:
            if spec.name not in self._specs:
                self._order.append(spec.name)
                # Keep the first spec object: the bytes a session first
                # advertised stay the bytes it advertises.  A changed remote
                # schema is picked up by the next session, not by an in-place
                # rewrite that would invalidate every cached call.
                self._specs[spec.name] = spec
                added.append(spec.name)
            self._last_task[spec.name] = self._task
        self._last_added = tuple(added)

        # The current task's selection is never evicted (the model needs the
        # tools it was chosen for), even if ``limit`` exceeds ``cap``.
        ceiling = max(1, int(self.cap), int(limit))
        while len(self._order) > ceiling:
            victims = [
                name
                for name in self._order
                if self._last_task.get(name, 0) < self._task
            ]
            if not victims:
                break
            oldest = min(
                victims,
                key=lambda name: (self._last_task.get(name, 0), self._order.index(name)),
            )
            self._forget(oldest)

        return tuple(self._specs[name] for name in self._order)

    def mark_expanded(self, names: tuple[str, ...] | list[str]) -> None:
        """Remember tools the kernel expanded, for later tasks in the session."""

        known = set(self._order)
        self.expanded.update(
            str(name) for name in names if str(name) in known
        )

    def reset(self, session_id: str = "") -> None:
        self.session_id = str(session_id)
        self._order.clear()
        self._specs.clear()
        self._last_task.clear()
        self._task = 0
        self._last_added = ()
        self.expanded.clear()

    def _forget(self, name: str) -> None:
        self._order.remove(name)
        self._specs.pop(name, None)
        self._last_task.pop(name, None)
        self.expanded.discard(name)
