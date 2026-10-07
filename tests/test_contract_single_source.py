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
    """引擎与校验层必须经 ensure/build 构建或读取契约。"""

    import xenon.engine.evidence_gate as evidence_gate
    import xenon.engine.react_engine as react_engine
    import xenon.repl.difficulty_estimator as difficulty_estimator

    # 这些模块的源码里必须出现契约入口（防止有人改回自行分类）。
    source = (
        Path(react_engine.__file__).read_text(encoding="utf-8")
        + Path(evidence_gate.__file__).read_text(encoding="utf-8")
        + Path(difficulty_estimator.__file__).read_text(encoding="utf-8")
    )
    assert "ensure_turn_contract" in source
    assert "build_turn_contract" in source
