"""Guard test: retired multi-engine vocabulary must not come back (#294).

The check lives in ``scripts/check_engine_vocabulary.py`` so the same rule is
available to a human ("what is left?") and to CI (fail the build).  This test
is the CI half.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _guard():
    spec = importlib.util.spec_from_file_location(
        "check_engine_vocabulary", ROOT / "scripts" / "check_engine_vocabulary.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_user_facing_text_does_not_use_the_retired_vocabulary() -> None:
    found = _guard().violations()
    rendered = [f"{rel}:{lineno} -> {text}" for rel, lineno, text in found]
    assert not rendered, rendered


def test_the_guard_catches_a_regression(tmp_path: Path) -> None:
    """The rule must actually fire, otherwise it is decoration."""
    guard = _guard()
    module = tmp_path / "xenon" / "fake.py"
    module.parent.mkdir(parents=True)
    module.write_text('X = "切换思考范式"\n', encoding="utf-8")

    found = guard.violations(module.parent)

    assert found, "a retired phrase in a string literal must be reported"
