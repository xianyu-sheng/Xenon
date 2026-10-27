"""Permission/scope guard tests (#201 first slice)."""

from __future__ import annotations

from tests.kernel.test_north_star import (
    FakeCatalog,
    FakeGateway,
    ScriptedStrategy,
    _calc_spec,
)
from xenon.kernel.contracts import (
    Action,
    Budget,
    CallTool,
    Capability,
    Finish,
    ModelView,
    PolicyDecisionKind,
    ReceiptStatus,
    TaskSpec,
    Think,
    ToolIntent,
    ToolReceipt,
)
from xenon.kernel.controller import VerificationController
from xenon.kernel.policies import (
    PermissionMode,
    PermissionPolicy,
    ScopedGrant,
)
from xenon.kernel.runtime import LoopRuntime
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


def _view(workspace: str = ".") -> ModelView:
    spec = TaskSpec(task_id="t1", objective="x", workspace=workspace)
    return ModelView(
        task=spec,
        system="",
        tools=(),
        working_set=(),
        debt=(),
        budget=Budget(),
        input_seq=0,
        hash="h",
    )


def _call(
    capability: Capability, **params: object
) -> Action:
    return CallTool(ToolIntent(capability=capability, params=params))


def _decision(policy: PermissionPolicy, action: Action, workspace: str = "."):
    return policy.check(_view(workspace), action)


def test_read_is_allowed_and_read_only_mode_blocks_edit(tmp_path) -> None:
    read = _decision(PermissionPolicy(), _call(Capability.READ, file_path="a.py"))
    assert read.kind is PolicyDecisionKind.ALLOW

    policy = PermissionPolicy(mode=PermissionMode.READ_ONLY, workspace=str(tmp_path))
    edit = _decision(policy, _call(Capability.EDIT, file_path="a.py"), str(tmp_path))
    assert edit.kind is PolicyDecisionKind.VETO


def test_workspace_edit_is_allowed_but_escape_is_not(tmp_path) -> None:
    policy = PermissionPolicy(workspace=str(tmp_path))
    inside = _decision(
        policy,
        _call(Capability.EDIT, files=[{"path": "calc/add.py", "content": "x"}]),
        str(tmp_path),
    )
    assert inside.kind is PolicyDecisionKind.ALLOW

    outside = _decision(
        policy, _call(Capability.EDIT, file_path="../outside.py"), str(tmp_path)
    )
    assert outside.kind is PolicyDecisionKind.VETO


def test_verify_allows_real_test_commands_only(tmp_path) -> None:
    policy = PermissionPolicy(workspace=str(tmp_path))
    real = _decision(
        policy,
        _call(Capability.VERIFY, command="python -m pytest tests/test_a.py -q"),
        str(tmp_path),
    )
    assert real.kind is PolicyDecisionKind.ALLOW

    injected = _decision(
        policy,
        _call(
            Capability.VERIFY,
            command="python -m pytest tests/test_a.py; rm -rf .",
        ),
        str(tmp_path),
    )
    assert injected.kind is PolicyDecisionKind.VETO


def test_verify_single_token_command_is_not_a_path(tmp_path) -> None:
    action = _call(Capability.VERIFY, command="custom-verify")
    default = PermissionPolicy(workspace=str(tmp_path))
    assert _decision(default, action, str(tmp_path)).kind is PolicyDecisionKind.VETO

    approving = PermissionPolicy(workspace=str(tmp_path), allow_approval=True)
    assert (
        _decision(approving, action, str(tmp_path)).kind
        is PolicyDecisionKind.REQUIRE_APPROVAL
    )


def test_structured_test_paths_do_not_authorize_a_command(tmp_path) -> None:
    action = _call(
        Capability.VERIFY,
        command="custom-verify",
        test_paths=[str(tmp_path / "tests" / "test_a.py")],
    )
    default = PermissionPolicy(workspace=str(tmp_path))
    assert _decision(default, action, str(tmp_path)).kind is PolicyDecisionKind.VETO
    approving = PermissionPolicy(workspace=str(tmp_path), allow_approval=True)
    assert (
        _decision(approving, action, str(tmp_path)).kind
        is PolicyDecisionKind.REQUIRE_APPROVAL
    )


def test_structured_test_paths_are_allowed_inside_workspace(tmp_path) -> None:
    policy = PermissionPolicy(workspace=str(tmp_path))
    action = _call(
        Capability.VERIFY,
        test_paths=[str(tmp_path / "tests" / "test_a.py")],
        test_framework="pytest",
    )
    assert _decision(policy, action, str(tmp_path)).kind is PolicyDecisionKind.ALLOW

    escaped = _call(
        Capability.VERIFY,
        test_paths=["../outside/test_a.py"],
    )
    assert (
        _decision(policy, escaped, str(tmp_path)).kind
        is PolicyDecisionKind.VETO
    )


def test_vcs_commit_allowed_but_push_and_reset_are_not(tmp_path) -> None:
    policy = PermissionPolicy(workspace=str(tmp_path))
    commit = _decision(
        policy, _call(Capability.VCS, command="git commit -m fix"), str(tmp_path)
    )
    assert commit.kind is PolicyDecisionKind.ALLOW

    for command in (
        "git push origin main",
        "git reset --hard",
        "git commit -m x; touch pwned",
    ):
        denied = _decision(
            policy, _call(Capability.VCS, command=command), str(tmp_path)
        )
        assert denied.kind is PolicyDecisionKind.VETO, command


def test_remote_requires_a_grant(tmp_path) -> None:
    policy = PermissionPolicy(workspace=str(tmp_path))
    denied = _decision(policy, _call(Capability.REMOTE), str(tmp_path))
    assert denied.kind is PolicyDecisionKind.VETO


def test_scoped_grant_expires(tmp_path) -> None:
    now = [100.0]
    grant = ScopedGrant(
        capability=Capability.VCS,
        target="git push",
        expires_at=200.0,
    )
    policy = PermissionPolicy(
        workspace=str(tmp_path), grants=(grant,), clock=lambda: now[0]
    )
    action = _call(Capability.VCS, command="git push origin main")

    assert _decision(policy, action, str(tmp_path)).kind is PolicyDecisionKind.ALLOW
    now[0] = 200.0
    assert _decision(policy, action, str(tmp_path)).kind is PolicyDecisionKind.VETO


class DeniedGateway:
    def execute(self, call, services) -> ToolReceipt:
        return ToolReceipt(
            receipt_id="denied-1",
            call_id="denied-call-1",
            task_id="task-1",
            tool=call.spec_name,
            status=ReceiptStatus.DENIED,
            error="policy denied at gateway",
        )


def test_gateway_denial_writes_receipt_and_policy_event(tmp_path) -> None:
    ledger = Ledger.in_memory("task-1")
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=DeniedGateway(),
        controller=VerificationController(),
        ledger=ledger,
    )

    result = runtime.run(
        _calc_spec(),
        ScriptedStrategy(
            [CallTool(ToolIntent(capability=Capability.EDIT)), Think("stop")]
        ),
    )

    assert result.accepted is False
    assert ledger.read(EventKind.TOOL_RECEIPT)
    assert any(
        event.kind is EventKind.POLICY_DECISION
        and "policy denied" in str(event.payload.get("reason"))
        for event in ledger.read()
    )


def test_veto_prevents_gateway_execution(tmp_path) -> None:
    ledger = Ledger.in_memory("task-1")
    policy = PermissionPolicy(workspace=str(tmp_path))
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
        policies=(policy,),
    )
    runtime_spec = _calc_spec()
    runtime_spec = type(runtime_spec)(
        task_id=runtime_spec.task_id,
        objective=runtime_spec.objective,
        workspace=str(tmp_path),
    )

    result = runtime.run(
        runtime_spec,
        ScriptedStrategy(
            [_call(Capability.EDIT, file_path="../outside.py"), Think("no")]
        ),
    )

    assert result.accepted is False
    assert result.phase.value == "blocked"
    assert not ledger.read(EventKind.TOOL_RECEIPT)
    assert any(
        event.kind is EventKind.POLICY_DECISION
        and event.payload.get("kind") == "veto"
        for event in ledger.read()
    )


def test_allow_approval_returns_require_approval_and_grants(tmp_path) -> None:
    policy = PermissionPolicy(
        workspace=str(tmp_path), allow_approval=True, approval_ttl_seconds=30
    )
    shell = _call(Capability.SHELL, command="echo hello")
    decision = _decision(policy, shell, str(tmp_path))
    assert decision.kind is PolicyDecisionKind.REQUIRE_APPROVAL
    assert decision.ttl_seconds == 30
    assert "echo hello" in decision.scope

    grants = policy.approve(decision, shell)
    assert grants
    assert _decision(policy, shell, str(tmp_path)).kind is PolicyDecisionKind.ALLOW
    policy.revoke(grants)
    assert (
        _decision(policy, shell, str(tmp_path)).kind
        is PolicyDecisionKind.REQUIRE_APPROVAL
    )

    destructive = _call(Capability.SHELL, command="rm -rf build")
    assert (
        _decision(policy, destructive, str(tmp_path)).kind
        is PolicyDecisionKind.VETO
    )
    remote = _call(Capability.REMOTE, server="docs")
    remote_decision = _decision(policy, remote, str(tmp_path))
    assert remote_decision.kind is PolicyDecisionKind.REQUIRE_APPROVAL
    assert policy.approve(remote_decision, remote)


def test_push_remains_veto_even_with_approval(tmp_path) -> None:
    policy = PermissionPolicy(workspace=str(tmp_path), allow_approval=True)
    for command in (
        "git push origin main",
        "git -C repo push origin main",
        "bash -c 'git push origin main'",
    ):
        assert (
            _decision(
                policy,
                _call(Capability.SHELL, command=command),
                str(tmp_path),
            ).kind
            is PolicyDecisionKind.VETO
        ), command
    assert (
        _decision(
            policy,
            _call(Capability.VCS, command="git push origin main"),
            str(tmp_path),
        ).kind
        is PolicyDecisionKind.VETO
    )
    assert (
        _decision(
            policy,
            _call(Capability.VERIFY, command="git push origin main"),
            str(tmp_path),
        ).kind
        is PolicyDecisionKind.VETO
    )


def test_read_only_allows_remote_only_with_approval(tmp_path) -> None:
    read_only = PermissionPolicy(
        mode=PermissionMode.READ_ONLY,
        workspace=str(tmp_path),
        allow_approval=True,
    )
    remote = _call(Capability.REMOTE, tool_name="github:search")
    assert (
        _decision(read_only, remote, str(tmp_path)).kind
        is PolicyDecisionKind.REQUIRE_APPROVAL
    )
    shell = _call(Capability.SHELL, command="echo hi")
    assert (
        _decision(read_only, shell, str(tmp_path)).kind
        is PolicyDecisionKind.VETO
    )
    without = PermissionPolicy(
        mode=PermissionMode.READ_ONLY, workspace=str(tmp_path)
    )
    assert (
        _decision(without, remote, str(tmp_path)).kind
        is PolicyDecisionKind.VETO
    )


def test_allow_approval_does_not_weaken_read_only(tmp_path) -> None:
    policy = PermissionPolicy(
        mode=PermissionMode.READ_ONLY,
        workspace=str(tmp_path),
        allow_approval=True,
    )
    action = _call(Capability.SHELL, command="echo hello")
    assert (
        _decision(policy, action, str(tmp_path)).kind
        is PolicyDecisionKind.VETO
    )


class Approval:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.calls = []

    def request(self, view, action, decision) -> bool:
        self.calls.append((action, decision))
        return self.allowed


def _approval_runtime(tmp_path, approval):
    ledger = Ledger.in_memory("task-1")
    policy = PermissionPolicy(
        workspace=str(tmp_path),
        allow_approval=True,
    )
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
        policies=(policy,),
        approval=approval,
    )
    spec = TaskSpec(
        task_id="task-1",
        objective="run custom verification",
        workspace=str(tmp_path),
    )
    action = CallTool(
        ToolIntent(
            capability=Capability.VERIFY,
            params={"command": "custom-verify"},
        )
    )
    result = runtime.run(
        spec, ScriptedStrategy([action, Finish("done")])
    )
    return policy, ledger, result


def test_runtime_approval_allows_once_and_revokes_grant(tmp_path) -> None:
    approval = Approval(True)
    policy, ledger, result = _approval_runtime(tmp_path, approval)

    assert approval.calls
    assert ledger.read(EventKind.USER_APPROVAL)
    assert any(
        event.kind is EventKind.POLICY_DECISION
        and event.payload.get("kind") == "require_approval"
        for event in ledger.read()
    )
    assert ledger.read(EventKind.TOOL_RECEIPT)
    assert not policy.grants
    assert result.phase.value in {"finalized", "delivered"}


def test_runtime_approval_denial_blocks_before_gateway(tmp_path) -> None:
    approval = Approval(False)
    policy, ledger, result = _approval_runtime(tmp_path, approval)

    assert ledger.read(EventKind.USER_APPROVAL)[0].payload["approved"] is False
    assert not ledger.read(EventKind.TOOL_RECEIPT)
    assert result.accepted is False
    assert result.phase.value == "blocked"
    assert not policy.grants


def test_runtime_without_approver_fails_closed(tmp_path) -> None:
    policy, ledger, result = _approval_runtime(tmp_path, None)

    event = ledger.read(EventKind.USER_APPROVAL)[0]
    assert event.payload["approved"] is False
    assert "no approval provider" in event.payload["reason"]
    assert not ledger.read(EventKind.TOOL_RECEIPT)
    assert result.accepted is False
    assert not policy.grants


class ExplodingApproval:
    def request(self, view, action, decision) -> bool:
        raise RuntimeError("ui crashed")


def test_approval_without_target_fails_closed(tmp_path) -> None:
    ledger = Ledger.in_memory("task-1")
    policy = PermissionPolicy(workspace=str(tmp_path), allow_approval=True)
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
        policies=(policy,),
        approval=Approval(True),
    )
    result = runtime.run(
        _calc_spec(),
        ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.REMOTE)),
                Finish("done"),
            ]
        ),
    )
    assert result.accepted is False
    assert result.phase.value == "blocked"
    assert not ledger.read(EventKind.TOOL_RECEIPT)
    assert "could not be scoped" in str(
        ledger.read(EventKind.STATE_TRANSITION)[-1].payload.get("reason")
    )
    assert not policy.grants


def test_approval_provider_exception_fails_closed(tmp_path) -> None:
    policy, ledger, result = _approval_runtime(
        tmp_path, ExplodingApproval()
    )
    event = ledger.read(EventKind.USER_APPROVAL)[0]
    assert event.payload["approved"] is False
    assert "approval provider failed" in event.payload["reason"]
    assert not ledger.read(EventKind.TOOL_RECEIPT)
    assert result.accepted is False
    assert not policy.grants
