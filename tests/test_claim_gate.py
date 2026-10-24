"""Deterministic claim verification and criterion-level goal audit."""

from __future__ import annotations

from types import SimpleNamespace

from xenon.engine.claim_gate import (
    CLAIM_COMMAND_PASSED,
    CLAIM_COMMIT_CREATED,
    CLAIM_FILE_WRITTEN,
    CLAIM_TEST_ARTIFACT,
    CLAIM_WORKTREE_CLEAN,
    REJECTED,
    VERIFIED,
    ClaimBaseline,
    audit_goal,
    claims_for_step,
    record_claim_verdicts,
    verify_step_claims,
)
from xenon.engine.task_state import TaskState


def _call(
    tool: str,
    params: dict | None = None,
    *,
    success: bool = True,
    summary: str = "ok",
    metadata: dict | None = None,
    result_id: str = "r1",
) -> SimpleNamespace:
    return SimpleNamespace(
        tool_name=tool,
        params=params or {},
        success=success,
        result_summary=summary,
        metadata=metadata or {},
        result_id=result_id,
    )


def test_step_claims_cover_write_verify_commit_and_clean() -> None:
    write = claims_for_step(
        {
            "id": "2",
            "kind": "edit",
            "requires_write": True,
            "target_paths": ["src/app.py"],
        }
    )
    verify = claims_for_step(
        {
            "id": "3",
            "kind": "verify",
            "verify_cmd": "python -m pytest tests/test_app.py -q",
        }
    )
    commit = claims_for_step({"id": "4", "kind": "commit"})
    clean = claims_for_step({"id": "5", "kind": "clean"})

    assert [claim.kind for claim in write] == [CLAIM_FILE_WRITTEN]
    assert verify[0].kind == CLAIM_COMMAND_PASSED
    assert verify[0].subject == "python -m pytest tests/test_app.py -q"
    assert commit[0].kind == CLAIM_COMMIT_CREATED
    assert clean[0].kind == CLAIM_WORKTREE_CLEAN


def test_claim_uses_only_calls_after_the_step_baseline(tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", encoding="utf-8")
    old = _call(
        "write_file",
        {"file_path": "src/app.py"},
        result_id="old",
    )
    claim = claims_for_step(
        {
            "id": "1",
            "kind": "edit",
            "requires_write": True,
            "target_paths": ["src/app.py"],
        }
    )[0]

    stale = verify_step_claims(
        [claim],
        calls=[old],
        state=TaskState(objective="x"),
        baseline=ClaimBaseline(calls_start=1),
        workspace_root=tmp_path,
    )
    assert stale[0].status == REJECTED
    assert "没有匹配" in stale[0].actual

    current = _call(
        "write_file",
        {"file_path": "src/app.py"},
        result_id="new",
    )
    fresh = verify_step_claims(
        [claim],
        calls=[old, current],
        state=TaskState(objective="x"),
        baseline=ClaimBaseline(calls_start=1),
        workspace_root=tmp_path,
    )
    assert fresh[0].status == VERIFIED
    assert "new" in fresh[0].evidence_ids


def test_command_claim_requires_success_and_zero_exit() -> None:
    claim = claims_for_step(
        {"id": "3", "kind": "verify", "verify_cmd": "pytest -q"}
    )[0]

    failed = verify_step_claims(
        [claim],
        calls=[
            _call(
                "command",
                {"command": "pytest -q"},
                success=True,
                summary="1 failed, 2 passed",
                metadata={"exit_code": 1},
            )
        ],
        state=TaskState(objective="x"),
        baseline=ClaimBaseline(),
    )
    assert failed[0].status == REJECTED

    passed = verify_step_claims(
        [claim],
        calls=[
            _call(
                "command",
                {"command": "python -m pytest -q"},
                summary="2 passed",
                metadata={"exit_code": 0},
            )
        ],
        state=TaskState(objective="x"),
        baseline=ClaimBaseline(),
    )
    assert passed[0].status == VERIFIED


def test_commit_claim_rejects_an_old_commit() -> None:
    claim = claims_for_step({"id": "4", "kind": "commit"})[0]
    state = TaskState(objective="x", commit="abc1234")
    verdicts = verify_step_claims(
        [claim],
        calls=[
            _call(
                "git",
                {"git_command": "commit -m done"},
                summary="[main abc1234] done",
            )
        ],
        state=state,
        baseline=ClaimBaseline(commit_before="abc1234"),
    )
    assert verdicts[0].status == REJECTED
    assert "没有产生新的 commit" in verdicts[0].actual


def test_test_artifact_claim_rejects_empty_content(tmp_path) -> None:
    claim = claims_for_step(
        {
            "id": "6",
            "kind": "edit",
            "requires_write": True,
            "target_paths": ["tests/test_app.py"],
        },
        requires_new_test=True,
    )
    assert [c.kind for c in claim] == [
        CLAIM_FILE_WRITTEN,
        CLAIM_TEST_ARTIFACT,
    ]
    test_claim = claim[1]
    path = tmp_path / "tests" / "test_app.py"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    verdict = verify_step_claims(
        [test_claim],
        calls=[
            _call(
                "write_file",
                {"file_path": "tests/test_app.py"},
                metadata={"content_chars": 0},
            )
        ],
        state=TaskState(objective="x"),
        baseline=ClaimBaseline(),
        workspace_root=tmp_path,
    )
    assert verdict[0].status == REJECTED
    assert "空" in verdict[0].reason


def test_goal_audit_reports_stale_verification_as_a_criterion() -> None:
    state = TaskState(
        objective="fix",
        intent="write_code",
        execution_level=3,
        steps=[
            {
                "id": "3",
                "task": "verify",
                "status": "completed",
                "kind": "verify",
                "verify_cmd": "pytest -q",
                "plan_version": 1,
            }
        ],
        artifacts=["src/app.py"],
        tests=["pytest -q"],
        verified_revision=0,
        artifacts_revision=1,
        required=["verify"],
    )

    failures = audit_goal(state)
    assert any(v.claim.claim_id == "goal:verify" and v.status == REJECTED for v in failures)
    assert any("过期" in v.actual for v in failures)

    state.verified_revision = 1
    passed = audit_goal(state)
    assert all(v.status == VERIFIED for v in passed)


def test_claim_verdicts_replay_into_task_state() -> None:
    from xenon.engine.context import AgentContext
    from xenon.engine.evidence_runtime import (
        EventKind,
        EvidenceLedger,
        EvidenceSource,
        LifecyclePhase,
    )
    from xenon.engine.task_state_projection import TaskStateProjection

    state = TaskState(objective="x", execution_level=3)
    ledger = EvidenceLedger("run-claims")
    ledger.append(
        LifecyclePhase.TASK,
        EventKind.TASK_FACT,
        EvidenceSource.ENGINE,
        {"engine": "fake", "task_id": state.task_id},
    )
    claim = claims_for_step({"id": "1", "kind": "commit"})[0]
    verdict = SimpleNamespace(
        claim=claim,
        status=REJECTED,
        actual="no commit",
        reason="missing",
        evidence_ids=[],
        to_dict=lambda: {
            **claim.to_dict(),
            "status": REJECTED,
            "actual": "no commit",
            "reason": "missing",
            "evidence_ids": [],
        },
    )
    record_claim_verdicts(ledger, [verdict], task_id=state.task_id)
    context = AgentContext(
        {"_task_state": state.to_dict(), "_evidence_ledger": ledger}
    )
    TaskStateProjection(state, context).attach(ledger, replay=True).close()

    assert state.claims[0]["claim_id"] == "step:1:commit_created:"
    assert state.claim_failures()[0]["status"] == REJECTED
    assert state.maybe_complete() is False


def test_plan_step_claim_failure_triggers_in_place_repair() -> None:
    from xenon.engine.context import AgentContext
    from xenon.engine.plan_execute_engine import PlanExecuteEngine
    from xenon.engine.tool_tracker import ToolExecutionTracker

    state = TaskState(
        objective="verify feature",
        execution_level=3,
        steps=[
            {
                "id": "1",
                "task": "run the verification command",
                "kind": "verify",
                "verify_cmd": "pytest -q",
                "status": "pending",
                "plan_version": 1,
            }
        ],
    )
    ctx = AgentContext({"_task_state": state.to_dict()})
    tracker = ToolExecutionTracker()
    engine = PlanExecuteEngine(["model"], max_steps=0)
    calls = {"count": 0}

    def fake_execute(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            tracker.record(
                "command",
                {"command": "pytest -q"},
                True,
                "2 passed",
                metadata={"exit_code": 0},
            )
        return "done"

    engine._execute_step_with_llm = fake_execute  # type: ignore[method-assign]
    results = engine._run_serial(
        [
            {
                "id": "1",
                "task": "run the verification command",
                "tool": None,
                "kind": "verify",
                "verify_cmd": "pytest -q",
                "params": {},
            }
        ],
        "verify feature",
        ctx,
        tracker,
        1,
    )

    assert calls["count"] == 2
    assert results[0]["status"] == "ok"
    final = TaskState.from_context(ctx)
    assert final is not None
    final.update_from_calls(tracker.calls)
    assert final.tests == ["pytest -q"]
    assert final.steps[0]["status"] == "completed"


def test_delivery_runtime_uses_goal_audit_for_targeted_repair() -> None:
    from xenon.engine.callbacks import EngineCallback
    from xenon.engine.context import AgentContext
    from xenon.engine.result import TerminationReason
    from xenon.engine.task_runtime import run_with_delivery
    from xenon.engine.tool_tracker import ToolExecutionTracker

    class _Engine:
        def __init__(self) -> None:
            self.callback = EngineCallback()
            self._last_tracker = ToolExecutionTracker()
            self._last_termination_reason = None
            self.prompts: list[str] = []

        def run(self, user_input, context=None, ctx_mgr=None):
            self.prompts.append(user_input)
            if len(self.prompts) > 1:
                self._last_tracker.record(
                    "command",
                    {"command": "pytest -q"},
                    True,
                    "1 passed",
                    metadata={"exit_code": 0},
                )
            return "done"

    state = TaskState(
        objective="verify",
        execution_level=3,
        steps=[
            {
                "id": "1",
                "task": "run verify",
                "kind": "verify",
                "verify_cmd": "pytest -q",
                "status": "completed",
                "plan_version": 1,
            }
        ],
    )
    context = AgentContext({"_task_state": state.to_dict()})
    engine = _Engine()
    result = run_with_delivery(engine, "finish", context, max_delivery_retries=2)

    final = TaskState.from_context(context)
    assert result == "done"
    assert result.reason is TerminationReason.FINAL
    assert final is not None
    assert final.status == "completed"
    assert final.tests == ["pytest -q"]
    assert "目标条件" in engine.prompts[1]
    assert "goal:step:1:verify" in engine.prompts[1]


def test_goal_audit_failure_is_reported_after_bounded_retries() -> None:
    from xenon.engine.callbacks import EngineCallback
    from xenon.engine.context import AgentContext
    from xenon.engine.result import TerminationReason
    from xenon.engine.task_runtime import run_with_delivery
    from xenon.engine.tool_tracker import ToolExecutionTracker

    class _Engine:
        def __init__(self) -> None:
            self.callback = EngineCallback()
            self._last_tracker = ToolExecutionTracker()
            self._last_termination_reason = None
            self.calls = 0

        def run(self, user_input, context=None, ctx_mgr=None):
            self.calls += 1
            return "done"

    state = TaskState(
        objective="verify",
        execution_level=3,
        steps=[
            {
                "id": "1",
                "task": "run verify",
                "kind": "verify",
                "verify_cmd": "pytest -q",
                "status": "completed",
                "plan_version": 1,
            }
        ],
    )
    context = AgentContext({"_task_state": state.to_dict()})
    engine = _Engine()
    result = run_with_delivery(engine, "finish", context, max_delivery_retries=1)

    assert engine.calls == 2
    assert result.reason is TerminationReason.DELIVERY_INCOMPLETE
    assert "目标条件未通过" in result
    assert "goal:step:1:verify" in result


def test_confirmation_commit_step_does_not_require_a_new_commit() -> None:
    claims = claims_for_step(
        {
            "id": "2",
            "kind": "commit",
            "task": "确认提交存在且工作区干净",
        }
    )
    assert [c.kind for c in claims] == [
        "commit_present",
        "worktree_clean",
    ]

    state = TaskState(objective="x", commit="abc1234")
    verdicts = verify_step_claims(
        claims,
        calls=[],
        state=state,
        baseline=ClaimBaseline(commit_before="abc1234"),
    )
    assert verdicts[0].status == VERIFIED
    assert verdicts[1].status != REJECTED


def test_user_acceptance_criteria_enter_goal_audit() -> None:
    from xenon.engine.claim_gate import goal_claims

    state = TaskState(
        objective="修好这个 bug，直到 CI 通过",
        execution_level=3,
        artifacts=["src/app.py"],
        artifacts_revision=1,
        required=[],
    )
    claims = goal_claims(state)
    assert any(c.claim_id.startswith("goal:acceptance:") for c in claims)

    failures = audit_goal(state)
    assert any(
        c.claim.claim_id.startswith("goal:acceptance:")
        and c.status == REJECTED
        for c in failures
    )

    state.tests = ["pytest -q"]
    state.verified_revision = state.artifacts_revision
    passed = audit_goal(state)
    assert all(
        c.status == VERIFIED
        for c in passed
        if c.claim.claim_id.startswith("goal:acceptance:")
    )


def test_step_start_records_pending_claim_before_execution() -> None:
    from xenon.engine.context import AgentContext
    from xenon.engine.evidence_runtime import (
        EventKind,
        EvidenceLedger,
        EvidenceSource,
        LifecyclePhase,
    )
    from xenon.engine.plan_execute_engine import PlanExecuteEngine

    state = TaskState(
        objective="write",
        execution_level=3,
        steps=[],
        plan_version=2,
    )
    ledger = EvidenceLedger("run-pending")
    ledger.append(
        LifecyclePhase.TASK,
        EventKind.TASK_FACT,
        EvidenceSource.ENGINE,
        {"engine": "plan", "task_id": state.task_id},
    )
    ctx = AgentContext(
        {"_task_state": state.to_dict(), "_evidence_ledger": ledger}
    )
    engine = PlanExecuteEngine(["model"], max_steps=0)

    engine._record_step_started(
        ctx,
        "3",
        "write source",
        1,
        step={
            "id": "3",
            "task": "write source",
            "kind": "edit",
            "requires_write": True,
            "target_paths": ["src/app.py"],
        },
    )

    claims = [
        event for event in ledger.events if event.kind == EventKind.CLAIM.value
    ]
    assert len(claims) == 1
    assert claims[0].payload["status"] == "pending"
    assert claims[0].payload["plan_version"] == 2
    assert claims[0].source == EvidenceSource.ENGINE.value
