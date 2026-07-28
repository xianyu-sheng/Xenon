"""Shared terminal-input helpers.

The visible editor buffer is deliberately separate from the value submitted
to Xenon's conversation context.  Large bracketed pastes are represented by a
short token while the user is editing, then restored losslessly before routing
or prompt compilation sees the message.
"""

from __future__ import annotations

from collections import OrderedDict


PASTE_COMPACT_THRESHOLD = 1000


def normalize_input_newlines(text: str) -> str:
    """Use LF internally without changing any other user-authored content."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


class PastedTextStore:
    """Keep full pasted text behind compact, prompt-local display tokens."""

    def __init__(self, threshold: int = PASTE_COMPACT_THRESHOLD) -> None:
        if threshold < 0:
            raise ValueError("paste compact threshold must be non-negative")
        self.threshold = threshold
        self._blocks: OrderedDict[str, str] = OrderedDict()
        self._next_id = 1

    def reset(self) -> None:
        """Start a new prompt so tokens can never leak across user turns."""
        self._blocks.clear()
        self._next_id = 1

    def compact(self, text: str, *, occupied_text: str = "") -> str:
        """Return a visible paste token for long text, otherwise the text itself."""
        normalized = normalize_input_newlines(text)
        if len(normalized) < self.threshold:
            return normalized

        while True:
            token = f"[Pasted #{self._next_id} +{len(normalized):,} chars]"
            self._next_id += 1
            if token not in occupied_text and token not in normalized:
                break
        self._blocks[token] = normalized
        return token

    def expand(self, visible_text: str) -> str:
        """Restore each token once, preserving block order and semantic newlines."""
        expanded = visible_text
        for token, content in self._blocks.items():
            expanded = expanded.replace(token, content, 1)
        return normalize_input_newlines(expanded)

    def token_before_cursor(self, text: str, cursor: int) -> str | None:
        """Return the paste token ending at ``cursor``, if any."""
        for token in self._blocks:
            if cursor >= len(token) and text.startswith(token, cursor - len(token)):
                return token
        return None

    def token_after_cursor(self, text: str, cursor: int) -> str | None:
        """Return the paste token starting at ``cursor``, if any."""
        for token in self._blocks:
            if text.startswith(token, cursor):
                return token
        return None

    @property
    def has_blocks(self) -> bool:
        return bool(self._blocks)
