"""The mode catalog declares loop phases, not strategy classes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from xenon.kernel.strategies import LoopProfile
from xenon.modes import (
    DEFAULT_MODE,
    MODE_CATALOG,
    get_mode,
    loop_profile_for,
)

# name -> (plan, plan_review, step_scope, follow_plan, reflect_rounds)
EXPECTED = {
    "direct": (False, False, False, False, 0),
    "react": (False, False, False, False, 0),
    "plan-execute": (True, False, True, True, 0),
    "reflection": (False, False, False, False, 1),
    "plan-react": (True, False, True, False, 0),
    "plan-reflection": (True, True, True, False, 1),
    "react-reflection": (False, False, False, False, 3),
}


def test_every_mode_declares_one_loop_profile():
    assert list(MODE_CATALOG) == list(EXPECTED)
    for name, (plan, plan_review, step_scope, follow_plan, rounds) in EXPECTED.items():
        profile = MODE_CATALOG[name].loop
        assert isinstance(profile, LoopProfile)
        assert profile.plan is plan, name
        assert profile.plan_review is plan_review, name
        assert profile.step_scope is step_scope, name
        assert profile.follow_plan is follow_plan, name
        assert profile.reflect_rounds == rounds, name
        assert MODE_CATALOG[name].name == name
        assert MODE_CATALOG[name].description


def test_no_mode_is_legacy_only():
    """One loop: every mode runs on the kernel."""

    assert all(spec.kernel_only for spec in MODE_CATALOG.values())
    assert all(loop_profile_for(name) is not None for name in MODE_CATALOG)


def test_reflection_budgets_are_ordered():
    once = loop_profile_for("reflection")
    multi = loop_profile_for("react-reflection")

    assert once is not None and multi is not None
    assert once.reflect_rounds == 1 and once.reflect_calls == 1
    assert multi.reflect_rounds > once.reflect_rounds
    assert multi.reflect_calls > once.reflect_calls


def test_plan_reflection_reviews_plan_and_steps():
    profile = loop_profile_for("plan-reflection")

    assert profile is not None
    assert profile.plan and profile.plan_review
    assert profile.step_scope and profile.step_reflect
    assert profile.follow_plan is False, "步骤要靠 actor 探索，而不是照计划读参数"


def test_blank_mode_resolves_to_default_but_unknown_does_not_guess():
    assert get_mode(None) is MODE_CATALOG[DEFAULT_MODE]
    assert get_mode("  REACT  ") is MODE_CATALOG["react"]
    assert get_mode("not-a-mode") is None
    assert loop_profile_for(None) is not None
    assert loop_profile_for("not-a-mode") is None


def test_model_registry_import_does_not_load_legacy_engine():
    """Reading /mode metadata must not import the 44-module engine skeleton."""

    code = """
import sys
import xenon.repl.model_registry

assert "xenon.engine.registry" not in sys.modules, sorted(sys.modules)
assert "xenon.engine.builtin_engines" not in sys.modules, sorted(sys.modules)
assert "reflection" in xenon.repl.model_registry.BUILTIN_MODES
print("ok")
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
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
