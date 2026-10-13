"""Semantic token catalog and default palette for the Xenon REPL.

Part of #112 / #113: freeze the token catalog and current default styles
before migrating inline color tags across the REPL.
"""

from __future__ import annotations

from rich.theme import Theme

TOKENS: tuple[str, ...] = (
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

DEFAULT_TOKEN_STYLES: dict[str, str] = {
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

DEFAULT_PT_STYLE: dict[str, str] = {
    # 输入区借鉴 Claude Code / pi 的轻量层次：线条定界，避免整块底色。
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


def default_rich_theme() -> Theme:
    """Return a Rich Theme instance built from DEFAULT_TOKEN_STYLES."""
    return Theme(DEFAULT_TOKEN_STYLES)
