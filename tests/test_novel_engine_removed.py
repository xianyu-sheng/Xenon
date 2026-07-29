from __future__ import annotations

import importlib.util

from xenon.repl.commands import COMMANDS
from xenon.repl.model_registry import BUILTIN_MODES


def test_novel_is_not_a_mode_command_or_importable_engine():
    assert "novel" not in BUILTIN_MODES
    assert "/novel" not in COMMANDS
    assert importlib.util.find_spec("xenon.engine.novel_engine") is None
    assert importlib.util.find_spec("xenon.engine.novel_manager") is None

