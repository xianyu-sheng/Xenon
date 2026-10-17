"""Lint contract: the ruff rule set is pinned, not inherited.

CI runs ``ruff check``, but if a rule is silently dropped from
``[tool.ruff.lint] select`` the check still passes while the contract
disappears.  #127 phase 1 pinned ``I`` (isort) and phase 2 pinned ``B``
(bugbear); this test keeps the pinned rules from being removed, while
allowing later phases (S) to extend the set — the same "freeze the contract,
not the tool version" pattern used by ``tests/test_step_budget.py`` and the
theme-token golden test.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

PINNED_RULES = {"E4", "E7", "E9", "F", "I", "B"}


def _ruff_select() -> list[str]:
    match = re.search(
        r"^select\s*=\s*(\[[^\]]*\])", PYPROJECT.read_text(encoding="utf-8"), re.M
    )
    assert match, "pyproject.toml has no [tool.ruff.lint] select list"
    return list(ast.literal_eval(match.group(1)))


def test_ruff_select_keeps_the_pinned_rule_contract():
    select = set(_ruff_select())
    missing = PINNED_RULES - select
    assert not missing, (
        f"pinned ruff rules were removed: {sorted(missing)} — the rule set is a "
        "contract (see pyproject and docs/ARCHITECTURE.md); update the contract "
        "and this test together if that is intentional"
    )


def test_ruff_select_uses_explicit_rule_names():
    """Guards against switching to ``select = ['ALL']`` or a dialect shortcut."""

    select = _ruff_select()
    assert select, "select list must not be empty"
    for rule in select:
        assert isinstance(rule, str) and rule.isalnum(), rule
