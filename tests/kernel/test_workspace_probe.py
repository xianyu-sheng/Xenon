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


def _ledger_with_probe(
    tmp_path, *, dirty, verified, seq_before=0, baseline=(), created=()
):
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
            "baseline_paths": list(baseline),
            "created_paths": list(created),
        },
    )
    return ledger


def _claim(
    since_seq: int = 0,
    kind: PropositionKind = PropositionKind.NO_STRAY_CHANGES,
    target: str = "",
) -> Claim:
    return Claim(
        id="e1",
        kind=kind,
        source=ClaimSource.USER,
        since_seq=since_seq,
        target=target,
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
        dirty_paths=("a.py",),
        verified_paths=("b.py",),
        head="deadbeef",
        baseline_paths=("c.py",),
        created_paths=("d.py",),
    )

    assert effect_from_payload(effect_to_payload(probe)) == probe


# ── 先于本轮存在的脏文件不是本轮的债（真实运行发现）────────────────
#
# 现场：仓库本来就有一批未提交改动。每一轮探针都把全部 dirty 路径与
# 「本轮写过的文件」相减，于是每轮都报 "worktree contains unverified
# paths: CHANGELOG.md, docs/…"，模型被诱导去重读、重改与任务无关的文件。
# 脏文件若在开跑前就已经脏、内容还没变，就不是本轮留下的。


def test_pre_existing_dirt_is_not_blamed_on_the_turn(tmp_path):
    ledger = _ledger_with_probe(
        tmp_path,
        dirty=["CHANGELOG.md", "calc.py"],
        verified=["calc.py"],
        baseline=["CHANGELOG.md"],
    )

    resolution = ClaimResolver().resolve(_claim(), ledger)

    assert resolution.status is ResolutionStatus.BOUND, resolution.reason


def test_the_baseline_only_covers_the_paths_it_names(tmp_path):
    ledger = _ledger_with_probe(
        tmp_path,
        dirty=["CHANGELOG.md", "notes.txt"],
        verified=[],
        baseline=["CHANGELOG.md"],
    )

    resolution = ClaimResolver().resolve(_claim(), ledger)

    assert resolution.status is ResolutionStatus.UNRESOLVED
    assert "notes.txt" in resolution.reason
    assert "CHANGELOG.md" not in resolution.reason


def test_a_previously_dirty_file_that_moves_becomes_stray(tmp_path):
    """Baseline means "unchanged since the turn began", not "forgiven"."""

    ledger = _ledger_with_probe(
        tmp_path, dirty=["calc.py"], verified=[], baseline=[]
    )

    assert ClaimResolver().resolve(_claim(), ledger).status is ResolutionStatus.UNRESOLVED


def test_inherited_dirt_does_not_prove_a_change(tmp_path):
    """A probe's baseline paths are observations, not receipts of work.

    Measured C10 run: ``e2e_scratch/buggy_calc.py`` was untracked *before*
    the turn.  The turn's first probe reported it dirty with an empty
    baseline, so ``file_changed(buggy_calc.py)`` was bound before the agent
    touched anything.  The probe now reports the paths it recorded as
    baseline; those cannot answer a change claim.
    """

    ledger = _ledger_with_probe(
        tmp_path,
        dirty=["e2e_scratch/buggy_calc.py"],
        verified=[],
        baseline=["e2e_scratch/buggy_calc.py"],
    )

    resolution = ClaimResolver().resolve(
        _claim(kind=PropositionKind.FILE_CHANGED, target="e2e_scratch/buggy_calc.py"),
        ledger,
    )

    assert resolution.status is not ResolutionStatus.BOUND


def _git(workspace, *args) -> None:
    import subprocess

    subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
    )


def test_the_git_probe_separates_baseline_dirt_from_turn_changes(tmp_path):
    from xenon.catalog.worktree import GitWorktreeProbe

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("x = 0\n", encoding="utf-8")
    _git(tmp_path, "add", "a.py")
    _git(
        tmp_path,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "-m",
        "init",
    )
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")  # dirty before

    probe = GitWorktreeProbe(tmp_path)
    first = probe.probe()
    assert first is not None and "a.py" in first.dirty_paths
    assert first.baseline_paths == ("a.py",), (
        "the first probe records the baseline and must report it: an empty "
        "baseline reads as 'all this dirt is this turn's work'"
    )
    assert first.created_paths == (), "nothing was created during the turn yet"

    second = probe.probe()
    assert second is not None
    assert second.baseline_paths == ("a.py",), "untouched dirt stays baseline"

    (tmp_path / "b.py").write_text("new = True\n", encoding="utf-8")
    third = probe.probe()
    assert third is not None
    assert "b.py" in third.dirty_paths
    assert "b.py" not in third.baseline_paths
    assert third.created_paths == ("b.py",), (
        "a new untracked file is this turn's creation, whatever wrote it"
    )

    (tmp_path / "a.py").write_text("x = 222\n", encoding="utf-8")
    fourth = probe.probe()
    assert fourth is not None
    assert "a.py" not in fourth.baseline_paths, (
        "a baseline path whose content moved belongs to this turn"
    )

    probe.begin_turn()
    fifth = probe.probe()
    assert fifth is not None, "the probe still answers after a turn boundary"
    # b.py was created by the previous turn, a.py was edited by it; both are
    # dirt inherited by the new turn, so both belong to its baseline.
    assert set(fifth.baseline_paths) == {"a.py", "b.py"}, (
        "a new turn records a fresh baseline and reports it"
    )


# ── 脚本写出的产物也是本轮证据（真实运行发现）──────────────────────
#
# 现场：任务要求「写脚本并运行它，脚本输出 sales_clean.csv」。脚本确实跑了、
# 文件确实生成了，但账本里只有一条 test_run 回执，没有 FsWrite 事实，
# 于是 file_created 义务被判 violated，交付结论是「未达成」。
# 探针看得到工作区：本轮新出现的未跟踪文件就是这一轮的产物。


def test_a_probe_proves_a_file_created_by_a_script(tmp_path):
    ledger = _ledger_with_probe(
        tmp_path,
        dirty=["out.csv"],
        verified=[],
        created=["out.csv"],
    )

    resolution = ClaimResolver().resolve(
        _claim(kind=PropositionKind.FILE_CREATED, target="out.csv"), ledger
    )

    assert resolution.status is ResolutionStatus.BOUND, resolution.reason


def test_a_baseline_file_is_not_created_by_this_turn(tmp_path):
    ledger = _ledger_with_probe(
        tmp_path,
        dirty=["old.csv", "out.csv"],
        verified=[],
        baseline=["old.csv"],
        created=["out.csv"],
    )

    resolution = ClaimResolver().resolve(
        _claim(kind=PropositionKind.FILE_CREATED, target="old.csv"), ledger
    )

    assert resolution.status is ResolutionStatus.UNRESOLVED


def test_a_probe_proves_a_file_changed_during_the_turn(tmp_path):
    ledger = _ledger_with_probe(
        tmp_path,
        dirty=["report.py", "CHANGELOG.md"],
        verified=[],
        baseline=["CHANGELOG.md"],
    )

    changed = ClaimResolver().resolve(
        _claim(kind=PropositionKind.FILE_CHANGED, target="report.py"), ledger
    )
    untouched = ClaimResolver().resolve(
        _claim(kind=PropositionKind.FILE_CHANGED, target="CHANGELOG.md"), ledger
    )

    assert changed.status is ResolutionStatus.BOUND, changed.reason
    assert untouched.status is ResolutionStatus.UNRESOLVED


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
