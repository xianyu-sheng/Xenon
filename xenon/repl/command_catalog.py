"""The command palette's single source of truth (#183).

The palette must never be a second menu maintained by hand: entries are derived
from the command registry that already exists (``COMMANDS``: name → description
and usage), so a newly registered command shows up automatically.  Grouping and
parameter hints are *declarative* — one table, testable — not string heuristics
scattered through the UI.

Pure except for the recents file, which is a tiny best-effort cache.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# Group order is display order: the palette reads top-down, most-used first.
GROUP_ORDER: tuple[str, ...] = (
    "会话",
    "规划",
    "权限",
    "模型",
    "证据",
    "工具",
    "配置",
    "其他",
)

# Explicit prefix → group table.  Longest prefix wins, so ``/sub-agent`` can be
# "工具" while ``/agent`` stays whatever it is.  Anything unmapped is "其他" —
# a new command is still discoverable, just not yet filed.
_GROUPS: tuple[tuple[str, str], ...] = (
    ("/plan", "规划"),
    ("/goal", "规划"),
    ("/mode", "模型"),
    ("/model", "模型"),
    ("/models", "模型"),
    ("/set_model", "模型"),
    ("/set-role", "模型"),
    ("/pool", "模型"),
    ("/provider", "模型"),
    ("/permissions", "权限"),
    ("/evidence", "证据"),
    ("/lessons", "证据"),
    ("/session", "会话"),
    ("/resume", "会话"),
    ("/load", "会话"),
    ("/save", "会话"),
    ("/fork", "会话"),
    ("/rewind", "会话"),
    ("/compact", "会话"),
    ("/clear", "会话"),
    ("/exit", "会话"),
    ("/quit", "会话"),
    ("/help", "会话"),
    ("/sub-agent", "工具"),
    ("/ask", "工具"),
    ("/code", "工具"),
    ("/run", "工具"),
    ("/skill", "工具"),
    ("/mcp", "工具"),
    ("/theme", "配置"),
    ("/cache", "配置"),
    ("/fix-cache", "配置"),
    ("/cost", "配置"),
    ("/memory", "配置"),
)


@dataclass(frozen=True, slots=True)
class CommandEntry:
    """One palette row: what it is, where it lives, what it takes."""

    name: str
    description: str
    usage: str = ""
    group: str = "其他"

    @property
    def param_hint(self) -> str:
        """The bracketed parameter stub from ``usage``, e.g. ``[on|off]``."""

        text = self.usage or ""
        start = text.find("[")
        if start < 0:
            return ""
        end = text.find("]", start)
        if end < 0:
            return text[start:][:40]
        return text[start : end + 1][:40]

    def search_text(self) -> str:
        return f"{self.name} {self.description}".casefold()

    def palette_meta(self) -> str:
        """Right-hand column: group + parameter hint.  Plain text only."""

        parts = [self.group]
        hint = self.param_hint
        if hint:
            parts.append(hint)
        return " · ".join(parts)


def group_for(name: str) -> str:
    """Longest matching prefix wins; unknown commands are still discoverable."""

    best = ""
    group = "其他"
    for prefix, candidate in _GROUPS:
        if name.startswith(prefix) and len(prefix) > len(best):
            best = prefix
            group = candidate
    return group


def build_catalog(commands: Mapping[str, Any] | None = None) -> tuple[CommandEntry, ...]:
    """Derive every palette row from the command registry.

    ``commands`` defaults to the live ``COMMANDS`` mapping, so the caller never
    keeps a list.  Entries are sorted by group order then name — deterministic,
    so tests can assert the exact palette.
    """

    if commands is None:
        from xenon.repl.commands import COMMANDS

        commands = COMMANDS
    entries = [
        CommandEntry(
            name=str(name),
            description=str(
                (meta or {}).get("description") if isinstance(meta, Mapping) else ""
            ),
            usage=str(
                (meta or {}).get("usage") if isinstance(meta, Mapping) else ""
            ),
            group=group_for(str(name)),
        )
        for name, meta in commands.items()
    ]
    order = {name: index for index, name in enumerate(GROUP_ORDER)}
    entries.sort(key=lambda item: (order.get(item.group, len(order)), item.name))
    return tuple(entries)


def score(entry: CommandEntry, query: str) -> int:
    """Fuzzy score, higher is better; 0 means "no match".

    Deliberately simple and deterministic: a name prefix beats a name
    subsequence, which beats a description hit.  That is enough to make
    ``perm`` land on ``/permissions`` without inventing a ranking model.
    """

    text = query.strip().casefold()
    if not text:
        return 1
    name = entry.name.casefold().lstrip("/")
    if name == text:
        return 1000
    if name.startswith(text):
        return 900
    # subsequence over the name ("prm" → permissions)
    position = 0
    for char in text:
        found = name.find(char, position)
        if found < 0:
            break
        position = found + 1
    else:
        return 600
    if text in entry.search_text():
        return 300
    return 0


def rank(
    entries: Sequence[CommandEntry],
    query: str = "",
    *,
    recents: Iterable[str] = (),
    limit: int = 0,
) -> tuple[CommandEntry, ...]:
    """Order the palette: recents first when idle, best match first when typing."""

    recent_index = {
        name: index for index, name in enumerate(name for name in recents)
    }
    if not query.strip():
        ordered = sorted(
            entries,
            key=lambda item: (
                0 if item.name in recent_index else 1,
                recent_index.get(item.name, 10_000),
            ),
        )
    else:
        scored = [
            (score(entry, query), -recent_index.get(entry.name, 10_000), index, entry)
            for index, entry in enumerate(entries)
        ]
        matching = [item for item in scored if item[0] > 0]
        matching.sort(key=lambda item: (-item[0], -item[1], item[2]))
        ordered = [item[3] for item in matching]
    if limit and limit > 0:
        return tuple(ordered[:limit])
    return tuple(ordered)


def enter_applies(
    text_before_cursor: str,
    completions: Sequence[str],
    index: int | None,
) -> str | None:
    """Which completion Enter should run, or ``None`` to just submit the text.

    prompt_toolkit's default Enter submits what is typed, so typing ``/perm``
    and pressing Enter runs a command that does not exist — the palette looks
    ignored.  Enter must execute the highlighted row instead (#183).

    Deliberately narrow: only a bare ``/command`` prefix (no space yet) is
    rewritten, so path completion keeps its normal "submit what I typed"
    behaviour.
    """

    text = text_before_cursor or ""
    if not text.startswith("/") or " " in text:
        return None
    if not completions:
        return None
    target = completions[index] if index is not None and 0 <= index < len(completions) else completions[0]
    return str(target) or None


def apply_palette_enter(buffer: Any) -> None:
    """Enter on an open palette: fill in the row, then submit.

    Extracted from the key binding so the behaviour is testable without a
    terminal: the binding is then a one-line call.
    """

    state = getattr(buffer, "complete_state", None)
    if state is not None:
        names = [str(item.text) for item in getattr(state, "completions", ())]
        index = getattr(state, "complete_index", None)
        target = enter_applies(
            buffer.document.text_before_cursor, names, index
        )
        if target is not None:
            buffer.text = target
            buffer.cursor_position = len(target)
    buffer.validate_and_handle()


def record_use(completer: Any, name: str) -> None:
    """Best-effort MRU bookkeeping: palette ordering must never break a command."""

    record = getattr(completer, "record_use", None)
    if not callable(record):
        return
    try:
        record(name)
    except Exception:  # noqa: BLE001 — ordering is cosmetic
        pass


class CommandRecents:
    """A tiny persistent MRU list for the palette (best effort, capped)."""

    def __init__(self, path: str | Path | None = None, *, limit: int = 12) -> None:
        self.path = Path(path) if path is not None else None
        self.limit = max(1, int(limit))
        self._names: list[str] = []
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, list):
            self._names = [str(item) for item in raw][: self.limit]

    def record(self, name: str) -> None:
        key = str(name)
        if not key:
            return
        self._names = [key] + [item for item in self._names if item != key]
        del self._names[self.limit :]
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._names, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass  # a palette ordering hint must never break a command

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._names)
