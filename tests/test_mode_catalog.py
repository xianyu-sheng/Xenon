"""The mode catalog is the shared source for /mode and kernel routing."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from xenon.modes import (
    DEFAULT_MODE,
    MODE_CATALOG,
    get_mode,
    kernel_strategy_for,
)

EXPECTED = {
    "direct": "act",
    "react": "act",
    "plan-execute": "plan",
    "reflection": None,
    "plan-react": None,
    "plan-reflection": None,
    "react-reflection": None,
}


def test_mode_catalog_names_and_strategies_are_explicit():
    assert list(MODE_CATALOG) == list(EXPECTED)
    assert {name: spec.kernel_strategy for name, spec in MODE_CATALOG.items()} == (
        EXPECTED
    )
    assert all(spec.name == name for name, spec in MODE_CATALOG.items())
    assert all(spec.description for spec in MODE_CATALOG.values())


def test_catalog_marks_legacy_only_modes():
    for name in ("reflection", "plan-react", "plan-reflection", "react-reflection"):
        assert MODE_CATALOG[name].legacy_only is True
    for name in ("direct", "react", "plan-execute"):
        assert MODE_CATALOG[name].legacy_only is False


def test_blank_mode_resolves_to_default_but_unknown_does_not_guess():
    assert get_mode(None) is MODE_CATALOG[DEFAULT_MODE]
    assert get_mode("  REACT  ") is MODE_CATALOG["react"]
    assert get_mode("not-a-mode") is None
    assert kernel_strategy_for(None) == "act"
    assert kernel_strategy_for("not-a-mode") is None


def test_model_registry_import_does_not_load_legacy_engine():
    """Reading /mode metadata must not import the 44-module engine skeleton."""

    code = """
import sys
import xenon.repl.model_registry

assert "xenon.engine.registry" not in sys.modules, sorted(sys.modules)
assert "xenon.engine.builtin_engines" not in sys.modules, sorted(sys.modules)
assert "reflection" in xenon.repl.model_registry.BUILTIN_MODES
"""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr or result.stdout
