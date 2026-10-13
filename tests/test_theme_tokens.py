"""Golden tests for REPL semantic token catalog and default styles.

Part of #112 / #113: freeze the token catalog and current default styles
so that the subsequent color tag migration remains strictly color-neutral.
"""

from __future__ import annotations

from rich.theme import Theme

from xenon.repl.theme import (
    DEFAULT_PT_STYLE,
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
)

FROZEN_DEFAULT_PT_STYLE: dict[str, str] = {
    "prompt": "bold #67e8f9",
    "input.rule": "#334155",
    "bottom-toolbar": "noreverse",
    "bottom-toolbar.text": "#94a3b8",
    "toolbar.separator": "#475569",
    "toolbar.model": "#cbd5e1",
    "toolbar.mode": "#c4b5fd",
    "toolbar.good": "bold #86efac",
    "toolbar.warning": "bold #fcd34d",
    "toolbar.danger": "bold #fda4af",
    "toolbar.notice": "bold #fde68a",
    "toolbar.muted": "#94a3b8",
    "toolbar.hint": "#64748b italic",
}


def test_tokens_catalog_completeness() -> None:
    assert TOKENS == FROZEN_TOKENS
    assert len(TOKENS) == 14
    assert len(set(TOKENS)) == 14
    assert set(TOKENS) == set(DEFAULT_TOKEN_STYLES.keys())


def test_default_token_styles_golden() -> None:
    assert DEFAULT_TOKEN_STYLES == FROZEN_DEFAULT_TOKEN_STYLES


def test_default_pt_style_verbatim() -> None:
    assert DEFAULT_PT_STYLE == FROZEN_DEFAULT_PT_STYLE


def test_default_rich_theme_builds() -> None:
    theme = default_rich_theme()
    assert isinstance(theme, Theme)
    for token in TOKENS:
        assert token in theme.styles
