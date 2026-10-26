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
