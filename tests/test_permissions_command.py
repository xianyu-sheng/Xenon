"""`/permissions` and the four-choice prompt (#373).

Two things matter here and both are about honesty:

* the list shows what is actually in force, with its source, so nobody has to
  guess why a prompt stopped appearing;
* "always" writes a rule **scoped to what was asked**, never to the whole
  capability — widening is a deliberate act of editing the file.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.kernel.contracts import Capability
from xenon.kernel.permission_rules import ALLOW, ASK, DENY, RuleSet, parse_rule
from xenon.kernel.session_grants import SESSION, GrantBook, grant_from_choice
from xenon.repl.approval_ux import APPROVAL_ALLOWED_ONCE, APPROVAL_REJECTED
from xenon.repl.command_groups.permissions import _cmd_permissions


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real ``~/.xenon/permissions.yaml``."""

    home = tmp_path / "_home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _repl(*, book=None, rules=None, workspace=None):
    return types.SimpleNamespace(
        _grant_book=book,
        _permission_rules=rules,
        project_ctx=types.SimpleNamespace(root=workspace) if workspace else None,
    )


def _call(repl, args: str = "") -> str:
    return _cmd_permissions(args=args, session_state={"_repl": repl})


# ── listing ─────────────────────────────────────────────────────────


def test_without_a_session_it_says_so() -> None:
    assert "交互会话" in _cmd_permissions(args="", session_state={})


def test_an_empty_state_explains_the_options() -> None:
    text = _call(_repl(book=GrantBook("/r"), rules=RuleSet()))

    assert "没有记住的授权" in text
    assert "A = 永远允许" in text


def test_session_grants_are_listed_first() -> None:
    book = GrantBook("/repo")
    book.add(
        grant_from_choice(
            SESSION, capability=Capability.SHELL, targets=("x",), workspace="/repo"
        )
    )

    text = _call(_repl(book=book, rules=RuleSet()))

    assert "会话级" in text
    assert "shell(*)" in text


def test_persisted_rules_are_listed_with_their_source() -> None:
    rules = RuleSet()
    rules.add(parse_rule("shell(git *)", decision=ALLOW, source="user"))
    rules.add(parse_rule("shell(rm *)", decision=DENY, source="project"))

    text = _call(_repl(rules=rules))

    assert "规则文件" in text
    assert "[user]" in text
    assert "[project]" in text


# ── removing ────────────────────────────────────────────────────────


def test_remove_needs_a_selector() -> None:
    assert "用法" in _call(_repl(rules=RuleSet()), "remove")


def test_remove_an_unknown_selector_reports_it() -> None:
    assert "没有找到" in _call(_repl(rules=RuleSet()), "remove shell(nope)")


def test_remove_a_rule_from_the_set(tmp_path) -> None:
    rules = RuleSet()
    rules.add(parse_rule("shell(git *)", decision=ALLOW))
    repl = _repl(rules=rules, workspace=tmp_path)

    text = _call(repl, "remove shell(git *)")

    assert "已撤销" in text
    assert rules.rules == []


def test_remove_a_session_grant_by_selector() -> None:
    book = GrantBook("/repo")
    book.add(
        grant_from_choice(
            SESSION, capability=Capability.SHELL, targets=("x",), workspace="/repo"
        )
    )

    text = _call(_repl(book=book, rules=None), "remove shell(*)")

    assert "已撤销" in text
    assert len(book) == 0


def test_clear_only_touches_the_session() -> None:
    book = GrantBook("/repo")
    book.add(
        grant_from_choice(
            SESSION, capability=Capability.SHELL, targets=("x",), workspace="/repo"
        )
    )
    rules = RuleSet()
    rules.add(parse_rule("verify", decision=ALLOW))

    text = _call(_repl(book=book, rules=rules), "clear")

    assert "已清空本会话 1 条" in text
    assert "规则文件未改动" in text
    assert len(rules.rules) == 1


def test_clear_without_a_book_is_harmless() -> None:
    assert "没有记住的授权" in _call(_repl(book=None), "clear")


# ── the prompt's four answers ───────────────────────────────────────


def _prompt_repl(choice: str, *, workspace=None, console=None):
    """A REPL-shaped object good enough to drive `_confirm_tool_approval`."""

    from xenon.repl.approval_ux import APPROVAL_ALLOWED_ONCE

    assert APPROVAL_ALLOWED_ONCE  # imported for clarity of intent
    rules = RuleSet()
    repl = types.SimpleNamespace(
        _workspace_root=lambda: str(workspace or "/repo"),
        _approval_rules=types.SimpleNamespace(
            is_allowed=lambda name: False, allow=lambda name: 600
        ),
        _boundary_approved_once=set(),
        _permission_prompt_lock=__import__("threading").RLock(),
        _terminal_activity=None,
        _active_callback=None,
        _approval_key=lambda name, params: f"{name}:{params}",
        _diff_preview=lambda name, params: "",
        _ask_approval_choice=lambda default="n", *, from_child=False: choice,
        agent_context=types.SimpleNamespace(get_security_advisories=lambda: []),
        _permission_rules=rules,
    )
    from contextlib import nullcontext

    from xenon.repl.approval_ux import ApprovalUxMixin

    repl._terminal_waiting = lambda detail: nullcontext()
    repl._persist_permission_rule = types.MethodType(
        ApprovalUxMixin._persist_permission_rule, repl
    )
    repl._confirm_tool_approval = types.MethodType(_confirm_impl(), repl)
    return repl


def _confirm_impl():
    from xenon.repl.approval_ux import ApprovalUxMixin

    return ApprovalUxMixin._confirm_tool_approval


class _Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *args, **kwargs) -> None:
        self.lines.append(" ".join(str(a) for a in args))


@pytest.fixture(autouse=True)
def _patch_console(monkeypatch):
    """A promptable REPL: console captured, assume_yes off, stdin looks like a TTY.

    Without all three the panel is never reached (it short-circuits on
    ``assume_yes`` or on a non-interactive stdin), and the test would silently
    assert nothing about the choices.
    """

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


def test_choice_a_remembers_for_the_session(tmp_path) -> None:
    repl = _prompt_repl("a", workspace=tmp_path)

    outcome = repl._confirm_tool_approval("command", {"command": "git log"})

    assert outcome == "allowed-once"
    assert repl._last_approval_scope == "session"


def test_choice_A_writes_a_rule_scoped_to_the_command(tmp_path, monkeypatch) -> None:
    import xenon.repl.approval_ux as ux

    written: list = []
    monkeypatch.setattr(ux, "console", _Console())
    repl = _prompt_repl("A", workspace=tmp_path)

    def fake_save(rule, path):
        written.append(rule)

    import xenon.kernel.permission_rules as pr

    monkeypatch.setattr(pr, "save_rule", fake_save)

    outcome = repl._confirm_tool_approval("command", {"command": "git log"})

    assert outcome == "allowed-once"
    assert repl._last_approval_scope == "always"
    assert written and written[0].pattern == "git log", written
    assert written[0].capability is Capability.SHELL


def test_choice_A_without_a_target_writes_nothing(tmp_path, monkeypatch) -> None:
    """No target means no honest scope, so no rule is invented."""

    import xenon.repl.approval_ux as ux

    console = _Console()
    monkeypatch.setattr(ux, "console", console)
    repl = _prompt_repl("A", workspace=tmp_path)

    repl._confirm_tool_approval("mcp_call", {})

    assert "unsupported" in " ".join(console.lines)


def test_choice_s_remembers_only_this_command(tmp_path) -> None:
    repl = _prompt_repl("s", workspace=tmp_path)

    outcome = repl._confirm_tool_approval("command", {"command": "pytest -q"})

    assert outcome == "allowed-once"
    assert repl._last_approval_scope == "scoped"


def test_choice_y_remembers_nothing(tmp_path) -> None:
    repl = _prompt_repl("y", workspace=tmp_path)

    repl._confirm_tool_approval("command", {"command": "git log"})

    assert repl._last_approval_scope == ""


def test_choice_n_rejects(tmp_path) -> None:
    repl = _prompt_repl("n", workspace=tmp_path)

    assert repl._confirm_tool_approval("command", {"command": "git log"}) == "rejected"


def test_choice_q_cancels(tmp_path) -> None:
    repl = _prompt_repl("q", workspace=tmp_path)

    assert repl._confirm_tool_approval("command", {"command": "git log"}) == "cancelled"


# ── the paths around the prompt ─────────────────────────────────────


class _Callback:
    """Stands in for the live-render callback that owns the terminal."""

    def __init__(self) -> None:
        self.resumed: list[str] = []

    def resume_after_prompt(self, tool_name: str, params: dict) -> None:
        self.resumed.append(tool_name)


@pytest.mark.parametrize("choice", ["a", "A", "s", "y", "n", "q"])
def test_every_answer_resumes_the_live_render(tmp_path, choice) -> None:
    """The panel must hand the terminal back whatever the answer was.

    Otherwise the spinner stays frozen and the run looks hung — the exact
    failure the terminal-activity tests were written for.
    """

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


def test_an_unwritable_rule_file_is_reported_not_raised(tmp_path, monkeypatch) -> None:
    import xenon.kernel.permission_rules as pr

    monkeypatch.setattr(
        pr, "save_rule", lambda rule, path: (_ for _ in ()).throw(OSError("readonly"))
    )
    repl = _prompt_repl("A", workspace=tmp_path)

    scope = repl._persist_permission_rule("command", {"command": "git log"})

    assert "could not write" in scope


def test_a_failed_rule_write_does_not_break_the_command(tmp_path) -> None:
    """`/permissions remove` must survive an unwritable settings file."""

    rules = RuleSet()
    rules.add(parse_rule("shell(git *)", decision=ALLOW))
    repl = _repl(rules=rules, workspace=tmp_path)

    import xenon.kernel.permission_rules as pr

    original = pr.default_rule_paths

    def boom(workspace=None):
        raise OSError("readonly")

    pr.default_rule_paths = boom
    try:
        text = _call(repl, "remove shell(git *)")
    finally:
        pr.default_rule_paths = original

    # The removal still succeeds in memory; only persisting is best effort.
    assert "已撤销" in text
    assert rules.rules == []


# ── the adapter forwards the layered rules to the policy ────────────


def test_rules_of_separates_deny_from_ask() -> None:
    from xenon.repl.runtime_adapter import RuntimeAdapter

    rules = RuleSet()
    rules.add(parse_rule("shell(rm *)", decision=DENY))
    rules.add(parse_rule("shell(npm i*)", decision=ASK))
    adapter = object.__new__(RuntimeAdapter)
    adapter.permission_rules = rules

    assert [rule.pattern for rule in adapter._rules_of("deny")] == ["rm *"]
    assert [rule.pattern for rule in adapter._rules_of("ask")] == ["npm i*"]


def test_rules_of_without_a_ruleset_is_empty() -> None:
    from xenon.repl.runtime_adapter import RuntimeAdapter

    adapter = object.__new__(RuntimeAdapter)
    adapter.permission_rules = None

    assert adapter._rules_of("deny") == ()


def test_rules_of_with_a_ruleset_missing_rules_is_empty() -> None:
    from xenon.repl.runtime_adapter import RuntimeAdapter

    adapter = object.__new__(RuntimeAdapter)
    adapter.permission_rules = types.SimpleNamespace()

    assert adapter._rules_of("ask") == ()


# ── child-mediated requests degrade to one-shot (no memory) ────────


def test_a_child_request_only_offers_ynq_and_remembers_nothing(tmp_path) -> None:
    """#373: answering a child's brokered request must not write any memory —
    no TTL rule, no allowed-once, no scope — even when the human says yes."""

    repl = _prompt_repl("y", workspace=tmp_path)
    ttl_rule: list[str] = []

    def record_allow(name: str) -> int:
        ttl_rule.append(name)
        return 600

    repl._approval_rules.allow = record_allow
    before = set(repl._boundary_approved_once)

    outcome = repl._confirm_tool_approval(
        "command", {"command": "echo hi"}, from_child=True
    )

    assert outcome == APPROVAL_ALLOWED_ONCE
    assert ttl_rule == [], "a child answer must not create a TTL rule"
    assert set(repl._boundary_approved_once) == before, (
        "a child answer must not create an allowed-once entry"
    )
    assert repl._last_approval_scope == ""


def test_a_child_request_cannot_invent_a_session_grant(tmp_path) -> None:
    """Even if the prompt were somehow answered 'a', a child request degrades
    to a deny — the human was never shown the remember choices."""

    repl = _prompt_repl("a", workspace=tmp_path)
    ttl_rule: list[str] = []

    def record_allow(name: str) -> int:
        ttl_rule.append(name)
        return 600

    repl._approval_rules.allow = record_allow

    outcome = repl._confirm_tool_approval(
        "command", {"command": "echo hi"}, from_child=True
    )

    assert outcome == APPROVAL_REJECTED
    assert ttl_rule == []


def test_a_child_request_still_inherits_the_parents_session_rule(tmp_path) -> None:
    """Inheritance is the *parent's* memory working for the child: a TTL rule
    the human already made answers the child's call without a new prompt."""

    repl = _prompt_repl("n", workspace=tmp_path)
    repl._approval_rules = types.SimpleNamespace(
        is_allowed=lambda name: True, allow=lambda name: 600
    )

    outcome = repl._confirm_tool_approval(
        "command", {"command": "echo hi"}, from_child=True
    )

    assert outcome == APPROVAL_ALLOWED_ONCE
