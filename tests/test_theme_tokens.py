"""Golden tests for REPL semantic token catalog and default styles.

Part of #112 / #113: freeze the token catalog and current default styles
so that the subsequent color tag migration remains strictly color-neutral.
"""

from __future__ import annotations

from rich.theme import Theme

from xenon.repl.theme import (
    DEFAULT_TOKEN_STYLES,
    TOKENS,
    default_rich_theme,
)

FROZEN_DEFAULT_TOKEN_STYLES: dict[str, str] = {
    "user": "bold #67e8f9",
    "assistant": "#bbf7d0",
    "system": "dim #facc15",
    "error": "bold #fda4af",
    "command": "bold #c4b5fd",
    "success": "bold #86efac",
    "warning": "bold #fcd34d",
    "info": "bold #67e8f9",
    "muted": "dim",
    "accent": "#c4b5fd",
    "heading": "bold #cbd5e1",
    "border": "#334155",
    "link": "underline #67e8f9",
    "code": "#fde68a",
    "logo.hot": "bold #f0f9ff",
    "logo.track": "#334155",
    "logo.runner": "bold #67e8f9",
    "logo.letter-a": "#67e8f9",
    "logo.letter-b": "#818cf8",
    "logo.letter-c": "#c4b5fd",
    "logo.dash": "#67e8f9",
    "logo.spark": "#67e8f9",
    "logo.ghost": "dim #94a3b8",
}

FROZEN_TOKENS: tuple[str, ...] = (
    "user",
    "assistant",
    "system",
    "error",
    "warning",
    "success",
    "info",
    "muted",
    "accent",
    "heading",
    "border",
    "link",
    "code",
    "command",
    "logo.hot",
    "logo.track",
    "logo.runner",
    "logo.letter-a",
    "logo.letter-b",
    "logo.letter-c",
    "logo.dash",
    "logo.spark",
    "logo.ghost",
)

def test_tokens_catalog_completeness() -> None:
    assert TOKENS == FROZEN_TOKENS
    assert len(TOKENS) == 23
    assert len(set(TOKENS)) == 23
    assert set(TOKENS) == set(DEFAULT_TOKEN_STYLES.keys())


def test_default_token_styles_golden() -> None:
    assert DEFAULT_TOKEN_STYLES == FROZEN_DEFAULT_TOKEN_STYLES


def test_default_rich_theme_builds() -> None:
    theme = default_rich_theme()
    assert isinstance(theme, Theme)
    for token in TOKENS:
        assert token in theme.styles


def test_default_palette_matches_live_repl() -> None:
    """Drift guard: repl.py and theme.py agree on the five core tokens."""
    from xenon.repl.repl import _theme

    for token in ("user", "assistant", "system", "error", "command"):
        assert str(_theme.styles[token]) == DEFAULT_TOKEN_STYLES[token]
