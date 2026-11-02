"""`no_stray_changes` bound to a deterministic worktree probe (#287).

Before this, the obligation could only ever say "no deterministic
worktree-clean fact yet" — the harness admitted it could not tell, even though
the host can read `git status`.  Three semantics matter:

1. a probe whose dirty paths are all accounted for → satisfied;
2. a probe with extra dirty paths → violated, naming them;
3. no probe at all (no VCS) → still abstain, because "cannot tell" must not
   look like "clean".
"""

from __future__ import annotations

import xenon.kernel  # noqa: F401  — breaks the ledger<->kernel import cycle
from xenon.kernel.contracts import (
    Claim,
    ClaimSource,
    PropositionKind,
    ResolutionStatus,
    WorkspaceProbe,
)
from xenon.kernel.controller import ClaimResolver
from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.store import Ledger


def _ledger_with_probe(tmp_path, *, dirty, verified, seq_before=0):
    ledger = Ledger(tmp_path / "t.jsonl", "t")
    for _ in range(seq_before):
        ledger.append(EventKind.THOUGHT, EventSource.LLM, {"text": "x"})
    # A probe is an observation, not a tool call: it has its own event so that
    # "plan-only executes no tools" stays literally true.
    ledger.append(
        EventKind.WORKSPACE_PROBED,
        EventSource.RUNTIME,
        {
            "type": "workspace_probe",
            "dirty_paths": list(dirty),
            "verified_paths": list(verified),
            "head": "abc",
        },
    )
    return ledger


def _claim(since_seq: int = 0) -> Claim:
    return Claim(
        id="e1",
        kind=PropositionKind.NO_STRAY_CHANGES,
        source=ClaimSource.USER,
        since_seq=since_seq,
    )


def test_only_accounted_paths_means_clean(tmp_path):
    ledger = _ledger_with_probe(tmp_path, dirty=["calc.py"], verified=["calc.py"])

    resolution = ClaimResolver().resolve(_claim(), ledger)

    assert resolution.status is ResolutionStatus.BOUND


def test_extra_dirty_paths_are_named(tmp_path):
    ledger = _ledger_with_probe(
        tmp_path, dirty=["calc.py", "notes.txt"], verified=["calc.py"]
    )

    resolution = ClaimResolver().resolve(_claim(), ledger)

    assert resolution.status is ResolutionStatus.UNRESOLVED
    assert "notes.txt" in resolution.reason
    assert "calc.py" not in resolution.reason.replace("unverified", "")


def test_no_probe_yet_still_abstains(tmp_path):
    ledger = Ledger(tmp_path / "t.jsonl", "t")
    ledger.append(EventKind.TASK_STARTED, EventSource.RUNTIME, {"objective": "x"})

    resolution = ClaimResolver().resolve(_claim(), ledger)

    assert resolution.status is ResolutionStatus.ABSTAINED
    assert "probe" in resolution.reason


def test_a_probe_before_the_obligation_still_counts(tmp_path):
    """A state predicate: the newest observation wins, whenever it was taken."""

    ledger = _ledger_with_probe(tmp_path, dirty=["a.py"], verified=[], seq_before=5)

    resolution = ClaimResolver().resolve(_claim(since_seq=5), ledger)

    assert resolution.status is ResolutionStatus.UNRESOLVED
    assert "a.py" in resolution.reason


def test_paths_are_normalised_before_comparison(tmp_path):
    ledger = _ledger_with_probe(
        tmp_path, dirty=["./calc.py"], verified=["calc.py"]
    )

    assert ClaimResolver().resolve(_claim(), ledger).status is ResolutionStatus.BOUND


def test_the_probe_effect_round_trips_through_the_ledger():
    from xenon.ledger.events import effect_from_payload, effect_to_payload

    probe = WorkspaceProbe(
        dirty_paths=("a.py",), verified_paths=("b.py",), head="deadbeef"
    )

    assert effect_from_payload(effect_to_payload(probe)) == probe


# ── 探针作为「验证型任务」的证据（真实运行发现）──────────────────────
#
# 只做验证的任务（"确认工作区干净"）不跑任何工具，于是被
# "至少一条成功工具回执" 规则挡住——明明它要的证据就在账上。
# 只有当任务真的声明了 no_stray_changes 义务时，探针才算证据。


def _spec_with(expect_kind):
    from xenon.kernel.contracts import (
        Obligation,
        ObligationSource,
        StepContract,
        StepKind,
        TaskMode,
        TaskSpec,
    )

    obligation = Obligation(
        id="e1",
        kind=expect_kind,
        source=ObligationSource.USER,
        required=True,
        since_seq=0,
    )
    step = StepContract(
        id="s1", kind=StepKind.THINK, intent="check", expects=(obligation,)
    )
    return TaskSpec(
        task_id="t", objective="o", workspace=".", mode=TaskMode.TASK
    ), step


def test_a_probe_counts_as_evidence_for_a_declared_clean_obligation():
    from tests.kernel.test_north_star import FakeCatalog, FakeGateway
    from xenon.kernel.contracts import PropositionKind
    from xenon.kernel.controller import VerificationController
    from xenon.kernel.runtime import LoopRuntime

    spec, step = _spec_with(PropositionKind.NO_STRAY_CHANGES)
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=Ledger.in_memory("t"),
    )
    runtime._spec = spec
    runtime._plan_steps = (step,)
    runtime._probed_signature = "head:():()"

    assert runtime._has_workspace_evidence() is True


def test_a_probe_does_not_count_for_an_unrelated_obligation():
    from tests.kernel.test_north_star import FakeCatalog, FakeGateway
    from xenon.kernel.contracts import PropositionKind
    from xenon.kernel.controller import VerificationController
    from xenon.kernel.runtime import LoopRuntime

    spec, step = _spec_with(PropositionKind.FILE_CHANGED)
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=Ledger.in_memory("t"),
    )
    runtime._spec = spec
    runtime._plan_steps = (step,)
    runtime._probed_signature = "head:():()"

    assert runtime._has_workspace_evidence() is False, (
        "an ordinary task must not borrow the probe as evidence"
    )


def test_without_a_probe_there_is_no_workspace_evidence():
    from tests.kernel.test_north_star import FakeCatalog, FakeGateway
    from xenon.kernel.contracts import PropositionKind
    from xenon.kernel.controller import VerificationController
    from xenon.kernel.runtime import LoopRuntime

    spec, step = _spec_with(PropositionKind.NO_STRAY_CHANGES)
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=Ledger.in_memory("t"),
    )
    runtime._spec = spec
    runtime._plan_steps = (step,)

    assert runtime._has_workspace_evidence() is False


# ── 整套测试通过 = 覆盖具体目标（真实运行发现）──────────────────────
#
# 现场：模型跑 `python -m pytest -q`（整仓），回执的 TestRun.target 却是
# 命令串本身，于是「tests/test_calc.py 通过」这条义务永远绑不上。


def test_a_whole_suite_run_covers_a_specific_target():
    from xenon.kernel.contracts import TestRun
    from xenon.kernel.controller import _test_matches

    whole = TestRun(
        target="",
        framework="pytest",
        tests_run=2,
        passed=2,
        exit_code=0,
        command="python -m pytest -q",
    )

    assert _test_matches(whole, "tests/test_calc.py") is True


def test_a_specific_run_still_does_not_cover_a_different_target():
    from xenon.kernel.contracts import TestRun
    from xenon.kernel.controller import _test_matches

    specific = TestRun(
        target="tests/test_other.py",
        framework="pytest",
        tests_run=1,
        passed=1,
        exit_code=0,
        command="python -m pytest tests/test_other.py -q",
    )

    assert _test_matches(specific, "tests/test_calc.py") is False


def test_the_gateway_records_a_whole_suite_run_as_no_target():
    from xenon.catalog.gateway import _test_target

    assert _test_target({"command": "python -m pytest -q"}, {}) == ""
    assert (
        _test_target({"command": "python -m pytest tests/test_a.py -q"}, {})
        == "tests/test_a.py"
    )
