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
    # 启动动画（跑者 Logo）专用语义 token：帧表只存语义色键，
    # 播放时按主题解析，/theme 换色后动画自动跟随。
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


def default_rich_theme() -> Theme:
    """Return a Rich Theme instance built from DEFAULT_TOKEN_STYLES."""
    return Theme(DEFAULT_TOKEN_STYLES)
