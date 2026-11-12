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


def test_merely_named_files_in_a_question_are_not_change_obligations() -> None:
    """A filename with no governing verb was named, not asked to change.

    Defaulting such files to ``file_changed`` manufactured obligations the
    run could never satisfy (e.g. "what is the difference between a.py and
    b.py?"); an unsatisfiable contract is worse than no contract (§1.5).
    """

    obligations = derive_obligations(
        "what is the difference between shapes.py and test_shapes.py?"
    )

    assert obligations == ()


def test_a_verb_after_the_filename_can_govern() -> None:
    """Chinese verbs follow their object; scanning only before lost them."""

    kinds = [(o.kind, o.target) for o in derive_obligations("shapes.py 需要修复")]

    assert kinds == [(PropositionKind.FILE_CHANGED, "shapes.py")]


def test_needs_work_is_a_write_request() -> None:
    kinds = [(o.kind, o.target) for o in derive_obligations("b.py also needs work")]

    assert kinds == [(PropositionKind.FILE_CHANGED, "b.py")]


def test_a_verb_after_a_sequential_connector_does_not_govern() -> None:
    """'c.py then run tests' — running tests is a new action, not what c.py
    must itself do; c.py stays in the fix list governed by 'Fix'."""

    kinds = [(o.kind, o.target) for o in derive_obligations(
        "Fix a.py, b.py, and c.py then run tests"
    )]

    assert (PropositionKind.FILE_CHANGED, "c.py") in kinds
    assert (PropositionKind.TEST_PASSED, "c.py") not in kinds


def test_comma_separated_file_lists_share_one_governing_verb() -> None:
    kinds = [(o.kind, o.target) for o in derive_obligations(
        "更新 a.py、b.py、c.py、d.py 四个文件的注释"
    )]

    assert kinds == [
        (PropositionKind.FILE_CHANGED, f"{name}.py")
        for name in "abcd"
    ]


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
    assert result.accepted is True
    assert any("calc.py" in str(item) for item in result.debt) or result.debt


def test_a_negated_file_never_becomes_a_change_obligation() -> None:
    """ "don't change calc.py" is an instruction to leave it alone.

    Demanding file_changed(calc.py) made compliance impossible for an agent
    that obeyed; the turn policy registers these paths as write-protected
    instead.
    """

    for task in (
        "don't change calc.py, fix util.py",
        "do not modify calc.py, fix util.py",
        "别改 calc.py，改 util.py",
        "保留 calc.py 不变，只改 util.py",
    ):
        files = {
            item.target
            for item in derive_obligations(task)
            if item.kind is PropositionKind.FILE_CHANGED
        }
        assert "calc.py" not in files, task
