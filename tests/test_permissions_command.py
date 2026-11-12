"""`/permissions` and the three-answer prompt (design doc §9).

Two things matter here and both are about honesty:

* the list shows what is actually in force, with the path it covers, so nobody
  has to guess why a prompt stopped appearing;
* "全部允许" is one tool + one execution path with a deadline.  It never widens
  to the whole capability, and it never survives the process.
"""

from __future__ import annotations

import types

import pytest

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.kernel.contracts import Capability
from xenon.kernel.session_grants import ALLOW, ONCE, GrantBook, grant_from_choice
from xenon.repl.approval_ux import APPROVAL_ALLOWED_ONCE, APPROVAL_REJECTED
from xenon.repl.command_groups.permissions import _cmd_permissions


def _repl(*, book=None, workspace=None):
    return types.SimpleNamespace(
        _grant_book=book,
        project_ctx=types.SimpleNamespace(root=workspace) if workspace else None,
    )


def _call(repl, args: str = "") -> str:
    return _cmd_permissions(args=args, session_state={"_repl": repl})


# ── listing ─────────────────────────────────────────────────────────


def test_without_a_session_it_says_so() -> None:
    assert "交互会话" in _cmd_permissions(args="", session_state={})


def test_an_empty_state_explains_the_options() -> None:
    text = _call(_repl(book=GrantBook("/r")))

    assert "没有记住的授权" in text
    assert "y = 本次允许" in text
    assert "a = 本会话都允许" in text


def test_grants_are_listed_with_the_path_they_cover() -> None:
    book = GrantBook("/repo")
    book.add(
        grant_from_choice(
            ALLOW, capability=Capability.SHELL, targets=("git log",), workspace="/repo"
        )
    )

    text = _call(_repl(book=book))

    assert "会话级" in text
    assert "shell: git log" in text
    assert "剩余 29 分钟" in text or "剩余 30 分钟" in text


def test_a_once_answer_is_marked_as_such() -> None:
    book = GrantBook("/repo")
    book.add(
        grant_from_choice(
            ONCE, capability=Capability.SHELL, targets=("git log",), workspace="/repo"
        )
    )

    assert "[本次]" in _call(_repl(book=book))


# ── removing ────────────────────────────────────────────────────────


def test_remove_needs_a_selector() -> None:
    assert "用法" in _call(_repl(book=GrantBook("/r")), "remove")


def test_remove_an_unknown_selector_reports_it() -> None:
    assert "没有找到" in _call(_repl(book=GrantBook("/r")), "remove shell:nope")


def test_remove_a_grant_by_capability_and_path() -> None:
    book = GrantBook("/repo")
    book.add(
        grant_from_choice(
            ALLOW, capability=Capability.SHELL, targets=("git log",), workspace="/repo"
        )
    )

    text = _call(_repl(book=book), "remove shell:git log")

    assert "已撤销" in text
    assert len(book) == 0


def test_clear_empties_the_book() -> None:
    book = GrantBook("/repo")
    book.add(
        grant_from_choice(
            ALLOW, capability=Capability.SHELL, targets=("git log",), workspace="/repo"
        )
    )

    text = _call(_repl(book=book), "clear")

    assert "已清空本会话 1 条" in text
    assert len(book) == 0


def test_clear_without_a_book_is_harmless() -> None:
    assert "没有记住的授权" in _call(_repl(book=None), "clear")


# ── the prompt's three answers ──────────────────────────────────────


def _prompt_repl(choice: str, *, workspace=None):
    """A REPL-shaped object good enough to drive `_confirm_tool_approval`."""

    repl = types.SimpleNamespace(
        _workspace_root=lambda: str(workspace or "/repo"),
        _grant_book=GrantBook(str(workspace or "/repo")),
        _last_approval_scope="",
        _permission_prompt_lock=__import__("threading").RLock(),
        _terminal_activity=None,
        _active_callback=None,
        _diff_preview=lambda name, params: "",
        _ask_approval_choice=lambda default="n", *, from_child=False: choice,
        agent_context=types.SimpleNamespace(get_security_advisories=lambda: []),
    )
    from contextlib import nullcontext

    from xenon.repl.approval_ux import ApprovalUxMixin

    repl._terminal_waiting = lambda detail: nullcontext()
    repl._approval_key = types.MethodType(ApprovalUxMixin._approval_key, repl)
    repl._current_target_note = types.MethodType(
        ApprovalUxMixin._current_target_note, repl
    )
    repl._remember_approval = types.MethodType(
        ApprovalUxMixin._remember_approval, repl
    )
    repl._push_grant_to_runtime = types.MethodType(
        ApprovalUxMixin._push_grant_to_runtime, repl
    )
    repl._confirm_tool_approval = types.MethodType(
        ApprovalUxMixin._confirm_tool_approval, repl
    )
    return repl


class _Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *args, **kwargs) -> None:
        self.lines.append(" ".join(str(a) for a in args))


@pytest.fixture(autouse=True)
def _patch_console(monkeypatch):
    """A promptable REPL: console captured, assume_yes off, stdin looks like a TTY."""

    import sys

    import xenon.repl.approval_ux as ux

    console = _Console()
    monkeypatch.setattr(ux, "console", console)
    monkeypatch.setattr(
        ux,
        "_config",
        lambda: types.SimpleNamespace(
            interaction=types.SimpleNamespace(assume_yes=False)
        ),
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    yield console


def test_choice_a_remembers_tool_and_path_for_the_session(tmp_path) -> None:
    repl = _prompt_repl("a", workspace=tmp_path)

    outcome = repl._confirm_tool_approval("command", {"command": "git log"})

    assert outcome == APPROVAL_ALLOWED_ONCE
    assert repl._last_approval_scope == "session"
    book = repl._grant_book
    assert len(book) == 1
    (grant,) = list(book)
    assert grant.capability is Capability.SHELL
    assert grant.targets == ("git log",)
    assert grant.one_shot is False
    assert grant.expires_at is not None


def test_choice_a_does_not_release_the_whole_capability(tmp_path) -> None:
    """The defect this replaces: one ``a`` press released every shell command."""

    repl = _prompt_repl("a", workspace=tmp_path)
    repl._confirm_tool_approval("command", {"command": "git log"})

    book = repl._grant_book
    assert book.active(str(tmp_path), Capability.SHELL, ["git log"])
    assert not book.active(str(tmp_path), Capability.SHELL, ["rm -rf /"])


def test_choice_y_remembers_nothing(tmp_path) -> None:
    """§9: 只放行这一次，下次再用仍需询问."""

    repl = _prompt_repl("y", workspace=tmp_path)

    outcome = repl._confirm_tool_approval("command", {"command": "git log"})

    assert outcome == APPROVAL_ALLOWED_ONCE
    assert repl._last_approval_scope == ""
    assert len(repl._grant_book) == 0


def test_choice_n_rejects(tmp_path) -> None:
    repl = _prompt_repl("n", workspace=tmp_path)

    assert repl._confirm_tool_approval("command", {"command": "git log"}) == "rejected"
    assert len(repl._grant_book) == 0


def test_choice_q_cancels(tmp_path) -> None:
    repl = _prompt_repl("q", workspace=tmp_path)

    assert repl._confirm_tool_approval("command", {"command": "git log"}) == "cancelled"


def test_the_panel_no_longer_offers_the_removed_answers(tmp_path, monkeypatch) -> None:
    """`A` (write a rule file) and `s` (scoped) are gone: §9 has three answers."""

    for legacy in ("A", "s"):
        repl = _prompt_repl(legacy, workspace=tmp_path)
        # The real reader would never return these; simulate it arriving anyway.
        repl._ask_approval_choice = (
            lambda default="n", *, from_child=False, _answer=legacy: _answer
        )

        outcome = repl._confirm_tool_approval("command", {"command": "git log"})

        assert outcome == APPROVAL_REJECTED, f"{legacy} must not authorize anything"
        assert len(repl._grant_book) == 0


# ── the paths around the prompt ─────────────────────────────────────


class _Callback:
    """Stands in for the live-render callback that owns the terminal."""

    def __init__(self) -> None:
        self.resumed: list[str] = []

    def resume_after_prompt(self, tool_name: str, params: dict) -> None:
        self.resumed.append(tool_name)


@pytest.mark.parametrize("choice", ["a", "y", "n", "q"])
def test_every_answer_resumes_the_live_render(tmp_path, choice) -> None:
    """The panel must hand the terminal back whatever the answer was."""

    repl = _prompt_repl(choice, workspace=tmp_path)
    callback = _Callback()
    repl._active_callback = callback

    repl._confirm_tool_approval("command", {"command": "git log"})

    assert callback.resumed == ["command"]


def test_a_keyboard_interrupt_at_the_prompt_cancels(tmp_path) -> None:
    repl = _prompt_repl("y", workspace=tmp_path)

    def boom(default="n", *, from_child=False):
        raise KeyboardInterrupt

    repl._ask_approval_choice = boom

    assert repl._confirm_tool_approval("command", {"command": "git log"}) == "cancelled"


# ── child-mediated requests degrade to one-shot (no memory) ────────


def test_a_child_request_remembers_nothing(tmp_path) -> None:
    """A child's "yes" applies to that call only — it may never accumulate."""

    repl = _prompt_repl("y", workspace=tmp_path)

    outcome = repl._confirm_tool_approval(
        "command", {"command": "echo hi"}, from_child=True
    )

    assert outcome == APPROVAL_ALLOWED_ONCE
    assert repl._last_approval_scope == ""
    assert len(repl._grant_book) == 0


def test_a_child_request_cannot_invent_a_session_grant(tmp_path) -> None:
    """Even if "a" arrived from a child, it must not be remembered."""

    repl = _prompt_repl("a", workspace=tmp_path)
    repl._ask_approval_choice = lambda default="n", *, from_child=False: "a"

    outcome = repl._confirm_tool_approval(
        "command", {"command": "echo hi"}, from_child=True
    )

    assert outcome == APPROVAL_ALLOWED_ONCE
    assert len(repl._grant_book) == 0


def test_the_child_prompt_offers_only_ynq(monkeypatch, tmp_path) -> None:
    """_ask_approval_choice(from_child=True) narrows the answer set to y/n/q."""

    from contextlib import nullcontext

    from xenon.repl.approval_ux import ApprovalUxMixin

    repl = types.SimpleNamespace(
        _permission_prompt_lock=None,
        _terminal_waiting=lambda detail: nullcontext(),
    )
    seen: dict = {}

    def ask(*args, **kwargs):
        seen.update(kwargs)
        return "y"

    monkeypatch.setattr("xenon.repl.approval_ux.Prompt.ask", ask)

    assert ApprovalUxMixin._ask_approval_choice(repl, from_child=True) == "y"
    assert seen["choices"] == ["y", "n", "q"]


def test_the_adult_prompt_offers_four_answers(monkeypatch, tmp_path) -> None:
    """§9's three answers, plus the turn-scoped one the panel adds.

    ``t`` exists because a real task pressed ``a`` for one command and the model
    then used four different spellings of the same intent — four panels for one
    intention.  ``t`` means "this capability, this turn only"; it is not a
    session grant and it does not outlive the turn.
    """

    from contextlib import nullcontext

    from xenon.repl.approval_ux import ApprovalUxMixin

    repl = types.SimpleNamespace(
        _permission_prompt_lock=None,
        _terminal_waiting=lambda detail: nullcontext(),
    )
    seen: dict = {}

    def ask(*args, **kwargs):
        seen.update(kwargs)
        return "y"

    monkeypatch.setattr("xenon.repl.approval_ux.Prompt.ask", ask)

    assert ApprovalUxMixin._ask_approval_choice(repl) == "y"
    assert seen["choices"] == ["y", "t", "a", "n", "q"]


# ── the turn-scoped answer (t) ───────────────────────────────────────


def _turn_repl(choice: str, *, workspace, turn: str = "turn-1"):
    repl = _prompt_repl(choice, workspace=workspace)
    repl._current_kernel_turn = turn
    return repl


def test_choice_t_covers_the_rest_of_the_turn(tmp_path) -> None:
    from xenon.kernel.session_grants import TURN_ALLOW, grant_from_choice

    repl = _turn_repl("t", workspace=tmp_path)
    outcome = repl._confirm_tool_approval("command", {"command": "git log"})

    assert outcome == APPROVAL_ALLOWED_ONCE
    assert repl._last_approval_scope == "turn"
    book = repl._grant_book
    assert len(book) == 1
    (grant,) = list(book)
    assert grant.targets == ("*",) and grant.turn == "turn-1"
    assert grant.capability is Capability.SHELL
    # Another command in the same turn is covered…
    assert book.consume(str(tmp_path), Capability.SHELL, ["ls -la"], turn="turn-1")
    # …but the same answer does not reach the next turn.
    book2 = GrantBook(str(tmp_path))
    book2.add(
        grant_from_choice(
            TURN_ALLOW,
            capability=Capability.SHELL,
            targets=("*",),
            workspace=str(tmp_path),
            turn="turn-1",
        )
    )
    assert book2.consume(str(tmp_path), Capability.SHELL, ["ls -la"], turn="turn-2") == ()


def test_choice_t_without_a_turn_does_not_authorize_anything_extra(tmp_path) -> None:
    """Falling back to session-wide would be the bug this answer exists to avoid."""

    repl = _prompt_repl("t", workspace=tmp_path)
    repl._current_kernel_turn = ""

    outcome = repl._confirm_tool_approval("command", {"command": "git log"})

    assert outcome == APPROVAL_ALLOWED_ONCE
    assert len(repl._grant_book) == 0


def test_switching_a_mode_is_persistent_and_only_clears_on_change() -> None:
    """/permissions writes the durable mode; turn_flow recomputes the level."""

    from xenon.kernel.policies import PermissionMode
    from xenon.repl.command_groups.permissions import _switch_mode
    from xenon.repl.execution_policy import PERMISSION_MODE_LEVELS
    from xenon.repl.runtime_adapter import permission_for_mode

    class Ctx(dict):
        def get(self, key, default=None):  # noqa: D401 - dict-like surface
            return super().get(key, default)

        def set(self, key, value):
            self[key] = value

    class Repl:
        def __init__(self):
            self.agent_context = Ctx()
            self._grant_book = None

    repl = Repl()
    message = _switch_mode(repl, "plan")
    assert message and "plan" in message
    assert repl.agent_context.get("_permission_mode") == "plan"
    assert PERMISSION_MODE_LEVELS["plan"] == 1

    # The four names mean four different kernel modes.
    assert permission_for_mode("plan", 3) is PermissionMode.READ_ONLY
    assert permission_for_mode("default", 2) is PermissionMode.SCOPED
    assert permission_for_mode("accept_edits", 2) is PermissionMode.AUTO_EDIT
    assert permission_for_mode("bypass", 2) is PermissionMode.BYPASS


def test_switching_to_the_same_mode_keeps_remembered_answers() -> None:
    from xenon.kernel.session_grants import GrantBook
    from xenon.repl.command_groups.permissions import _switch_mode

    class Ctx(dict):
        def get(self, key, default=None):
            return super().get(key, default)

        def set(self, key, value):
            self[key] = value

    class Repl:
        def __init__(self):
            self.agent_context = Ctx()
            self._grant_book = GrantBook(workspace="D:/ws")

    repl = Repl()
    repl._grant_book.add  # noqa: B018 - existence check
    from xenon.kernel.contracts import Capability
    from xenon.kernel.session_grants import ALLOW, grant_from_choice

    repl._grant_book.add(
        grant_from_choice(
            ALLOW, capability=Capability.EDIT, targets=["a.py"], workspace="D:/ws"
        )
    )
    repl.agent_context.set("_permission_mode", "accept_edits")
    before = len(repl._grant_book)
    _switch_mode(repl, "accept_edits")
    _switch_mode(repl, "accept_edits")  # same mode: not a boundary change
    assert len(repl._grant_book) == before


def test_the_reported_mode_is_the_stored_one_not_the_level() -> None:
    """``default`` and ``accept_edits`` share one level; the name must not.

    A level-only reverse mapping reported accept_edits as default, so the
    picker pre-selected the wrong row and pressing Enter switched the mode.
    """

    from xenon.repl.command_groups.permissions import _current_mode_name

    class Ctx(dict):
        def get(self, key, default=None):
            return super().get(key, default)

        def set(self, key, value):
            self[key] = value

    repl = types.SimpleNamespace(agent_context=Ctx())
    repl.agent_context.set("_execution_level", 2)
    repl.agent_context.set("_permission_mode", "accept_edits")

    assert _current_mode_name(repl) == "accept_edits"
