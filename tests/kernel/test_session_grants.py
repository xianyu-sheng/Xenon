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

    assert decision.reason.startswith("scoped grant:")


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
