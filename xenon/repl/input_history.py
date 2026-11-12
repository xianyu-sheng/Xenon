"""Persistent input history for the built-in line editor.

``prompt_toolkit``'s ``FileHistory`` went away with the TUI; this is the same
facility without the dependency.  Everything here is best effort: a read-only
home directory must never break the prompt.
"""

from __future__ import annotations

from pathlib import Path


class InputHistory:
    """A small MRU list with Up/Down navigation and a one-line-per-entry file."""

    def __init__(self, path: Path | None = None, *, limit: int = 500) -> None:
        self.path = path or (Path.home() / ".xenon" / "input_history.txt")
        self.limit = max(1, int(limit))
        self._entries: list[str] = []
        #: None means "not navigating"; otherwise an index into ``_entries``.
        self._cursor: int | None = None
        self._draft = ""
        self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        lines = raw.splitlines()
        entries: list[str] = []
        for line in lines:
            if not line.strip() or line.startswith("# "):
                continue
            # prompt_toolkit's FileHistory wrote "\n# <timestamp>\n+<entry>";
            # the same file is now the plain format.  Read both.
            entries.append(line[1:] if line.startswith("+") else line)
        self._entries = entries[-self.limit :]
        if len(lines) > self.limit * 4:
            self._rewrite()

    def _rewrite(self) -> None:
        """Compact the file into the plain one-line-per-entry format."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                "".join(entry + "\n" for entry in self._entries), encoding="utf-8"
            )
        except OSError:
            pass

    def add(self, text: str) -> None:
        entry = (text or "").strip("\n")
        if not entry.strip():
            return
        self._cursor = None
        if self._entries and self._entries[-1] == entry:
            return
        self._entries.append(entry)
        del self._entries[: -self.limit]
        if "\n" in entry:
            # One row per entry: a multi-line paste would be split into
            # unrelated single-line rows on the next load.
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(entry + "\n")
        except OSError:
            pass

    def previous(self, draft: str) -> str | None:
        if not self._entries:
            return None
        if self._cursor is None:
            self._draft = draft
            self._cursor = len(self._entries) - 1
        elif self._cursor > 0:
            self._cursor -= 1
        return self._entries[self._cursor]

    def next(self) -> str | None:
        if self._cursor is None:
            return None
        if self._cursor < len(self._entries) - 1:
            self._cursor += 1
            return self._entries[self._cursor]
        self._cursor = None
        return self._draft
