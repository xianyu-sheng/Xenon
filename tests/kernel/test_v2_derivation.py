"""#420: V2 must have something to verify.

`TaskSpec.obligations` existed but nothing ever filled it, so global coverage
was always empty: the task level never ran.  These tests pin the derivation and
the "disclose, never block before work" rule.
"""

from __future__ import annotations

from pathlib import Path

from xenon.harness.intake import derive_obligations
from xenon.kernel.contracts import PropositionKind


def test_files_and_a_test_runner_become_obligations() -> None:
    task = (
        "修改 calc.py 里的 div 和 mean，同时更新 test_calc.py 的断言，"
        "最后跑 pytest 验证。"
    )

    obligations = derive_obligations(task)

    kinds = [(item.kind, item.target) for item in obligations]
    assert (PropositionKind.FILE_CHANGED, "calc.py") in kinds
    assert (PropositionKind.FILE_CHANGED, "test_calc.py") in kinds
    assert (PropositionKind.TEST_PASSED, "test_calc.py") in kinds


def test_prose_requirements_are_not_faked_into_coverage() -> None:
    """No deterministic binding exists for prose, and inventing one would
    report coverage that does not exist."""
    obligations = derive_obligations("把代码改得更好，不要引入回归，注意可读性")

    assert obligations == ()


def test_documents_are_not_treated_as_code_changes() -> None:
    obligations = derive_obligations("更新 README.md 说明这个行为")

    assert all(item.target != "README.md" for item in obligations)


def test_the_same_file_is_only_counted_once() -> None:
    obligations = derive_obligations("改 calc.py，然后 review calc.py 再改 calc.py")

    files = [item for item in obligations if item.kind is PropositionKind.FILE_CHANGED]
    assert len(files) == 1


def test_a_bare_basename_is_not_a_second_obligation() -> None:
    """SmartBench #26 named the full path once, then ``common.py:188``.

    Deriving a third, bare ``common.py`` obligation made the run blocked on a
    file the task never asked anyone to change.
    """

    task = (
        "Fix smartbench/core/rules/common.py:188 and "
        "smartbench/cli/unified.py, then update tests/test_common.py."
    )

    files = [
        item.target
        for item in derive_obligations(task)
        if item.kind is PropositionKind.FILE_CHANGED
    ]
    assert files == [
        "smartbench/core/rules/common.py",
        "smartbench/cli/unified.py",
        "tests/test_common.py",
    ]


def test_obligations_are_bounded() -> None:
    task = " ".join(f"file{i}.py" for i in range(40))

    assert len(derive_obligations(task)) <= 12


def test_a_violated_task_obligation_blocks_delivery_not_execution(tmp_path: Path) -> None:
    """The rule: the agent runs; if the task level cannot confirm it, the run is
    *disclosed* as unverified.  Execution is never cut short."""
    from tests.kernel.test_north_star import ScriptedStrategy, _runtime
    from xenon.kernel.contracts import (
        Finish,
        Obligation,
        PropositionKind,
        TaskSpec,
    )
    from xenon.ledger.store import Ledger

    ledger = Ledger.in_memory("v2-gap")
    runtime = _runtime(ledger)
    spec = TaskSpec(
        task_id="v2-gap",
        objective="change calc.py",
        workspace=str(tmp_path),
        obligations=(
            Obligation(
                id="v2-1",
                kind=PropositionKind.FILE_CHANGED,
                target="calc.py",
            ),
        ),
    )

    result = runtime.run(spec, ScriptedStrategy([Finish("all done")]))

    # The run reached the audit (it was not stopped before doing anything)…
    kinds = {event.kind.value for event in ledger.read()}
    assert "audit_reported" in kinds
    # …and the gap was disclosed rather than silently accepted.
    assert result.accepted is False
    assert any("calc.py" in str(item) for item in result.debt) or result.debt
