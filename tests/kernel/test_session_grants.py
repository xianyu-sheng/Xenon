"""Session grants: ask once, record the human's decision, never ask again (#373).

The rules under test are all about *what a grant is not*:
  - it is not evidence (no claim, no verdict, no VERIFIED);
  - it is not transferable (workspace-scoped, capability-scoped);
  - it is not creatable by a model or a child.
"""

from __future__ import annotations

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.kernel.contracts import Capability, ToolIntent
from xenon.kernel.policies import PermissionMode, PermissionPolicy
from xenon.kernel.session_grants import (
    DENIED,
    ONCE,
    SCOPED,
    SESSION,
    GrantBook,
    grant_from_choice,
)
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


def _policy(workspace: str = "/repo", **kwargs) -> PermissionPolicy:
    return PermissionPolicy(
        mode=PermissionMode.AUTO_EDIT,
        workspace=workspace,
        allow_approval=True,
        **kwargs,
    )


def _shell(command: str) -> ToolIntent:
    return ToolIntent(capability=Capability.SHELL, params={"command": command})


# ── the four answers ────────────────────────────────────────────────


def test_once_creates_no_grant() -> None:
    assert (
        grant_from_choice(
            ONCE, capability=Capability.SHELL, targets=("x",), workspace="/repo"
        )
        is None
    )


def test_session_covers_the_whole_capability() -> None:
    grant = grant_from_choice(
        SESSION, capability=Capability.SHELL, targets=("x",), workspace="/repo"
    )

    assert grant is not None
    assert grant.scope == "*"
    assert grant.capability is Capability.SHELL


def test_scoped_covers_exactly_one_target() -> None:
    grant = grant_from_choice(
        SCOPED, capability=Capability.SHELL, targets=("pytest -q",), workspace="/r"
    )

    assert grant is not None and grant.scope == "pytest -q"


def test_scoped_with_several_targets_is_refused_not_widened() -> None:
    """Widening silently would authorize more than the human saw."""

    assert (
        grant_from_choice(
            SCOPED, capability=Capability.SHELL, targets=("a", "b"), workspace="/r"
        )
        is None
    )


def test_denied_is_not_a_grant() -> None:
    assert (
        grant_from_choice(
            DENIED, capability=Capability.SHELL, targets=("x",), workspace="/repo"
        )
        is None
    )


# ── the policy honours it ───────────────────────────────────────────


def test_a_session_grant_turns_prompts_into_allows() -> None:
    grant = grant_from_choice(
        SESSION, capability=Capability.SHELL, targets=("git log",), workspace="/repo"
    )
    policy = _policy()

    decision = policy.check_intent(_shell("git log -n 2"))

    assert decision.kind is not None
    assert "requires" in decision.reason  # without the grant: needs approval

    policy.grant((grant.as_scoped(),))
    decision = policy.check_intent(_shell("git log -n 2"))

    # A session grant is a recorded decision: the reason says where it came
    # from, so a replay can explain why this call was not asked (#373).
    assert decision.reason.startswith("session grant:")


def test_a_scoped_grant_covers_only_its_target() -> None:
    grant = grant_from_choice(
        SCOPED, capability=Capability.SHELL, targets=("pytest -q",), workspace="/repo"
    )
    policy = _policy()
    policy.grant((grant.as_scoped(),))

    assert policy.check_intent(_shell("pytest -q")).reason.startswith("scoped grant:")
    # A grant is not a blank cheque: another command is still not covered...
    assert not policy.check_intent(_shell("ls -la")).reason.startswith(
        "scoped grant:"
    )


def test_a_session_grant_cannot_authorize_a_destructive_command() -> None:
    """The safety rule outranks the human's "always": destructive stays vetoed."""

    grant = grant_from_choice(
        SESSION, capability=Capability.SHELL, targets=("x",), workspace="/repo"
    )
    policy = _policy()
    policy.grant((grant.as_scoped(),))

    decision = policy.check_intent(_shell("rm -rf /"))

    assert decision.reason == "destructive shell command"


def test_a_grant_does_not_cross_capabilities() -> None:
    grant = grant_from_choice(
        SESSION, capability=Capability.VERIFY, targets=("x",), workspace="/repo"
    )
    policy = _policy()
    policy.grant((grant.as_scoped(),))

    decision = policy.check_intent(_shell("anything"))

    assert not decision.reason.startswith("scoped grant:")


# ── the book ────────────────────────────────────────────────────────


def test_grants_are_scoped_to_one_workspace(tmp_path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    book = GrantBook(str(one))
    book.add(
        grant_from_choice(
            SESSION, capability=Capability.SHELL, targets=("x",), workspace=str(one)
        )
    )

    assert len(book.active(str(one))) == 1
    assert book.active(str(two)) == ()


def test_two_spellings_of_the_same_workspace_match(tmp_path) -> None:
    """A real probe broke here: MSYS `/tmp/x` vs the resolved Windows path."""

    book = GrantBook(str(tmp_path))
    book.add(
        grant_from_choice(
            SESSION,
            capability=Capability.SHELL,
            targets=("x",),
            workspace=str(tmp_path) + "/",
        )
    )

    assert len(book.active(str(tmp_path))) == 1


def test_an_identical_grant_replaces_instead_of_duplicating() -> None:
    book = GrantBook("/repo")
    grant = grant_from_choice(
        SESSION, capability=Capability.SHELL, targets=("x",), workspace="/repo"
    )
    book.add(grant)
    book.add(grant)

    assert len(book) == 1


def test_a_revoked_grant_stops_applying() -> None:
    book = GrantBook("/repo")
    book.add(
        grant_from_choice(
            SESSION, capability=Capability.SHELL, targets=("x",), workspace="/repo"
        )
    )

    removed = book.revoke(1)

    assert removed is not None
    assert book.active("/repo") == ()


def test_revoking_out_of_range_is_a_no_op() -> None:
    book = GrantBook("/repo")
    assert book.revoke(5) is None


def test_grants_round_trip_through_a_session_file() -> None:
    book = GrantBook("/repo")
    book.add(
        grant_from_choice(
            SESSION, capability=Capability.SHELL, targets=("x",), workspace="/repo"
        )
    )
    payload = book.to_payload()

    restored = GrantBook("/repo")
    restored.load(payload)

    assert len(restored) == 1
    assert restored.rows()[0]["capability"] == "shell"


def test_a_malformed_stored_grant_is_dropped_not_guessed() -> None:
    """Keeping a broken grant would authorize *something*; dropping costs a prompt."""

    book = GrantBook("/repo")
    book.load(
        [
            {"capability": "not-a-capability", "scope": "*", "workspace": "/repo"},
            {"capability": "shell", "scope": "", "workspace": "/repo"},
            {"capability": "shell", "scope": "*", "workspace": "/repo"},
            "garbage",
        ]
    )

    assert len(book) == 1


def test_loading_a_non_list_is_ignored() -> None:
    book = GrantBook("/repo")
    book.load({"nope": True})
    assert len(book) == 0


# ── through the runtime: the ledger records the decision ────────────


class _ScriptedApprover:
    def __init__(self, scope: str = SESSION) -> None:
        self.scope = scope
        self.last_scope = scope
        self.last_reason = "user chose always"
        self.calls = 0

    def request(self, view, action, decision):  # noqa: ANN001, ANN201
        self.calls += 1
        return True


def test_the_runtime_records_grant_added_and_hands_it_back(tmp_path) -> None:
    from tests.kernel.test_north_star import ScriptedStrategy, _runtime
    from xenon.kernel.contracts import CallTool, TaskSpec
    from xenon.ledger.events import EventKind
    from xenon.ledger.store import Ledger

    ledger = Ledger.in_memory("task-grant")
    policy = _policy(str(tmp_path))
    runtime = _runtime(ledger, policies=(policy,))
    runtime._approval = _ScriptedApprover()

    # Ask for a shell call that needs approval, then let the runtime proceed.
    action = CallTool(
        xenon.kernel.contracts.ToolIntent(
            capability=Capability.SHELL, params={"command": "git log"}
        )
    )
    runtime.run(
        TaskSpec(task_id="task-grant", objective="o", workspace=str(tmp_path)),
        ScriptedStrategy([action]),
    )

    events = [
        event
        for event in ledger.read()
        if event.kind is EventKind.GRANT_ADDED
    ]
    assert events, "the human's decision must be in the ledger"
    assert events[0].payload["answer"] == SESSION
    assert runtime.last_grant is not None


# ── inheritance and no-creation (children) ─────────────────────────


def test_a_child_run_uses_inherited_grants_without_asking(tmp_path) -> None:
    """The parent's grant answers the child's call; no approval is invoked."""

    from tests.kernel.test_north_star import ScriptedStrategy, _runtime
    from xenon.kernel.contracts import CallTool, TaskSpec

    grant = grant_from_choice(
        SESSION, capability=Capability.SHELL, targets=("git log",), workspace=str(tmp_path)
    )
    assert grant is not None
    ledger = Ledger.in_memory("child-inherit")
    # A policy seeded with the parent's grant, exactly what the child runner
    # does with ``session_grants``.
    policy = _policy(str(tmp_path), grants=(grant.as_scoped(),))
    runtime = _runtime(ledger, policies=(policy,))
    approver = _ScriptedApprover()
    runtime._approval = approver

    action = CallTool(
        xenon.kernel.contracts.ToolIntent(
            capability=Capability.SHELL, params={"command": "git log"}
        )
    )
    runtime.run(
        TaskSpec(task_id="child-inherit", objective="o", workspace=str(tmp_path)),
        ScriptedStrategy([action]),
    )

    assert approver.calls == 0, "an inherited grant must answer without asking"
    events = [
        event
        for event in ledger.read()
        if event.kind.value == "policy_decision"
    ]
    assert events and str(events[-1].payload.get("reason") or "").startswith(
        "session grant"
    )


def test_a_child_approval_answer_never_remembers(tmp_path) -> None:
    """The broker's answer is one-shot: no last_scope, so no GRANT_ADDED."""

    from tests.kernel.test_north_star import ScriptedStrategy, _runtime
    from xenon.kernel.contracts import CallTool, TaskSpec

    ledger = Ledger.in_memory("child-noremember")
    policy = _policy(str(tmp_path))
    runtime = _runtime(ledger, policies=(policy,))
    # The child-side broker has no ``last_scope`` attribute at all — the
    # kernel's ``_request_approval`` reads it and finds nothing.
    runtime._approval = _ScriptedApprover()
    runtime._approval.last_scope = None

    action = CallTool(
        xenon.kernel.contracts.ToolIntent(
            capability=Capability.SHELL, params={"command": "git log"}
        )
    )
    runtime.run(
        TaskSpec(task_id="child-noremember", objective="o", workspace=str(tmp_path)),
        ScriptedStrategy([action]),
    )

    events = [
        event for event in ledger.read() if event.kind is EventKind.GRANT_ADDED
    ]
    assert not events, "a child's approval must never write a grant"
    assert runtime.last_grant is None


def test_the_book_drops_grants_when_the_workspace_changes(tmp_path) -> None:
    """A grant made in repo A is inert in repo B (#373 acceptance)."""

    other = tmp_path / "other-repo"
    other.mkdir()
    book = GrantBook(workspace=str(tmp_path))
    grant = grant_from_choice(
        SESSION, capability=Capability.SHELL, targets=("x",), workspace=str(tmp_path)
    )
    assert grant is not None
    book.add(grant)

    assert book.active(str(tmp_path)) == (grant.as_scoped(),)
    assert book.active(str(other)) == ()


def test_the_child_spec_round_trips_grants(tmp_path) -> None:
    """A grant serialized into the child spec is restored by the worker."""

    from xenon.harness.child import _child_spec
    from xenon.harness.llm import ModelConfig

    grant = grant_from_choice(
        SCOPED, capability=Capability.VERIFY, targets=("pytest",), workspace=str(tmp_path)
    )
    assert grant is not None
    spec = _child_spec(
        child_id="child-spec",
        objective="o",
        workspace=str(tmp_path),
        model=ModelConfig(model_id="test/fake"),
        profile="kernel",
        ledger_dir=tmp_path / "ledger",
        permission=PermissionMode.AUTO_EDIT,
        max_steps=5,
        parent_task_id="parent",
        broker_dir=str(tmp_path / "broker"),
        session_grants=(grant,),
    )

    from xenon.harness.child import _grant_from_payload

    restored = [
        restored
        for raw in spec["session_grants"]
        if (restored := _grant_from_payload(raw)) is not None
    ]
    assert len(restored) == 1
    assert restored[0].capability is Capability.VERIFY
    assert restored[0].target == "pytest"
    assert restored[0].reason.startswith("scoped grant:")
# ── wire-format edge cases (fail-closed) ───────────────────────────

def test_spec_payload_falls_back_to_the_raw_scoped_grant() -> None:
    """_to_scoped passes a raw ScopedGrant through unchanged."""

    from xenon.harness.child import _to_scoped
    from xenon.kernel.policies import ScopedGrant

    scoped = ScopedGrant(capability=Capability.SHELL, target="git log", reason="x")
    assert _to_scoped(scoped) is scoped


def test_spec_payload_drops_malformed_grants() -> None:
    """A bad capability or empty target is dropped, never guessed."""

    from xenon.harness.child import _grant_from_payload

    assert _grant_from_payload({"capability": "not-a-cap", "target": "x"}) is None
    assert _grant_from_payload({"capability": "shell", "target": ""}) is None
    assert _grant_from_payload({"capability": "shell"}) is None
    assert _grant_from_payload("nonsense") is None


def test_spec_payload_skips_unrecognised_effects() -> None:
    """An unknown effect kind does not fail the whole grant."""

    from xenon.harness.child import _grant_from_payload
    from xenon.kernel.contracts import EffectKind
    from xenon.kernel.policies import ScopedGrant

    restored = _grant_from_payload(
        {
            "capability": "shell",
            "target": "git log",
            "effects": ["fs_delete", "not-a-kind"],
            "reason": "session grant: shell",
        }
    )
    assert restored is not None
    assert isinstance(restored, ScopedGrant)
    assert EffectKind.FS_DELETE in restored.effects
    assert len(restored.effects) == 1


def test_spec_payload_carries_effects_and_reason() -> None:
    """The happy path keeps effects and the recorded reason."""

    from xenon.harness.child import _grant_from_payload
    from xenon.kernel.contracts import EffectKind

    restored = _grant_from_payload(
        {
            "capability": "edit",
            "target": "calc.py",
            "effects": ["fs_delete"],
            "reason": "scoped grant: calc.py",
        }
    )
    assert restored is not None
    assert EffectKind.FS_DELETE in restored.effects
    assert restored.reason == "scoped grant: calc.py"
