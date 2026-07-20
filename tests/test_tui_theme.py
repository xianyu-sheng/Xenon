"""Regression tests for Xenon's interactive visual hierarchy."""

from __future__ import annotations

import io

from rich.console import Console

from xenon.repl.context_manager import ContextManager
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.status_bar import StatusBar


def _bar() -> StatusBar:
    return StatusBar(Console(file=io.StringIO()), ContextManager(), ModelRegistry())


def test_toolbar_has_branded_styled_fragments():
    fragments = _bar().get_toolbar_fragments()
    assert fragments[0] == ("class:toolbar.brand", "  XENON ")
    assert any(style == "class:toolbar.mode" for style, _ in fragments)
    assert any("context" in text for _, text in fragments)


def test_toolbar_promotes_compaction_warning():
    bar = _bar()
    bar.ctx_mgr.add_user_message("x" * 200_000)
    fragments = bar.get_toolbar_fragments()
    assert ("class:toolbar.danger", "  ⚠ /compact  ") in fragments
