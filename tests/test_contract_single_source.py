"""Phase 4a guard: the execution policy has exactly one home.

``classify_execution_policy`` may only live in ``execution_policy.py`` (its
definition) and ``turn_contract.py`` (the only builder).  Every other layer
must consume ``TurnContract`` — a regression here re-introduces the scattered
re-classification that made earlier behavior inconsistent.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {
    ROOT / "xenon" / "repl" / "execution_policy.py",
    ROOT / "xenon" / "repl" / "turn_contract.py",
}


def test_classify_execution_policy_has_a_single_home() -> None:
    offenders: list[str] = []
    for path in (ROOT / "xenon").rglob("*.py"):
        if path in ALLOWED:
            continue
        if "classify_execution_policy" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == [], (
        "只有 execution_policy.py / turn_contract.py 可以引用 "
        f"classify_execution_policy，违规文件: {offenders}"
    )


def test_removed_side_channels_do_not_come_back() -> None:
    """Phase 4b 删掉的旁路 hook 不允许恢复。"""
    forbidden = ("_input_requires_tools", "_TOOL_PATTERNS")
    offenders: list[str] = []
    for path in (ROOT / "xenon").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in source:
                offenders.append(f"{path.relative_to(ROOT)}: {token}")

    assert offenders == [], offenders


def test_contract_is_built_through_the_shared_entry_points() -> None:
    """The builder defines the entry points; the flow must consume them.

    This used to also require them in ``engine/evidence_gate.py``.  That module
    is now just the tool-level verdict type (the engine gate pipeline was
    retired with the kernel), so the guard follows the live consumers instead
    of a module that no longer builds contracts.
    """

    import xenon.repl.turn_contract as turn_contract
    import xenon.repl.turn_flow as turn_flow

    builder = Path(turn_contract.__file__).read_text(encoding="utf-8")
    flow = Path(turn_flow.__file__).read_text(encoding="utf-8")

    assert "def ensure_turn_contract(" in builder
    assert "def build_turn_contract(" in builder
    assert "build_turn_contract(" in flow
    # The flow consumes the built contract; it must not re-classify on its own.
    assert "classify_execution_policy" not in flow
