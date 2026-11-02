"""Pattern-based permission rules (#373).

The ordering is the safety property: **deny > ask > allow**.  A broad allow in a
user file must never unlock something a project file denies, and an "ask" rule
must outrank a remembered grant.
"""

from __future__ import annotations

import pytest

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.kernel.contracts import Capability, ToolIntent
from xenon.kernel.permission_rules import (
    ALLOW,
    ASK,
    DENY,
    PermissionRule,
    RuleSet,
    load_rules,
    parse_rule,
    save_rule,
)
from xenon.kernel.policies import PermissionMode, PermissionPolicy


def _shell(command: str) -> ToolIntent:
    return ToolIntent(capability=Capability.SHELL, params={"command": command})


# ── parsing ─────────────────────────────────────────────────────────


def test_a_rule_with_a_pattern_parses() -> None:
    rule = parse_rule("shell(pytest:*)", decision=ALLOW)

    assert rule.capability is Capability.SHELL
    assert rule.pattern == "pytest:*"


def test_a_bare_capability_means_everything_in_it() -> None:
    assert parse_rule("shell", decision=ALLOW).pattern == "*"


def test_an_unknown_capability_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_rule("nonsense(x)", decision=ALLOW)


def test_an_unbalanced_selector_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_rule("shell(x", decision=ALLOW)


# ── matching ────────────────────────────────────────────────────────


def test_a_pattern_is_matched_with_globs() -> None:
    rule = parse_rule("shell(git *)", decision=ALLOW)

    assert rule.matches(Capability.SHELL, ["git log"])
    assert not rule.matches(Capability.SHELL, ["rm -rf /"])


def test_a_rule_does_not_cross_capabilities() -> None:
    rule = parse_rule("verify", decision=ALLOW)

    assert not rule.matches(Capability.SHELL, ["pytest"])


def test_a_patterned_rule_never_authorizes_a_targetless_call() -> None:
    """Otherwise the pattern would silently widen to "anything"."""

    rule = parse_rule("shell(git *)", decision=ALLOW)

    assert not rule.matches(Capability.SHELL, [])


# ── precedence ──────────────────────────────────────────────────────


def test_deny_beats_allow() -> None:
    rules = RuleSet()
    rules.add(parse_rule("shell", decision=ALLOW))
    rules.add(parse_rule("shell(rm *)", decision=DENY))

    decision, rule = rules.evaluate(Capability.SHELL, ["rm -rf /"])

    assert decision == DENY
    assert rule.pattern == "rm *"


def test_ask_beats_allow() -> None:
    rules = RuleSet()
    rules.add(parse_rule("shell", decision=ALLOW))
    rules.add(parse_rule("shell(git push*)", decision=ASK))

    decision, _ = rules.evaluate(Capability.SHELL, ["git push origin main"])

    assert decision == ASK


def test_no_match_returns_none() -> None:
    rules = RuleSet()
    rules.add(parse_rule("verify", decision=ALLOW))

    assert rules.evaluate(Capability.SHELL, ["ls"]) is None


# ── the policy honours the rules ────────────────────────────────────


def _policy(**kwargs) -> PermissionPolicy:
    return PermissionPolicy(
        mode=PermissionMode.AUTO_EDIT, workspace="/repo", allow_approval=True, **kwargs
    )


def test_a_deny_rule_wins_over_a_broad_grant() -> None:
    """The exact hole that mattered: a wildcard grant must not beat a deny."""

    deny = parse_rule("shell(rm *)", decision=DENY)
    policy = _policy(
        grants=(parse_rule("shell", decision=ALLOW).as_grant(),),
        deny_rules=(deny,),
    )

    decision = policy.check_intent(_shell("rm -rf /tmp/x"))

    assert "denied by rule" in decision.reason


def test_a_deny_rule_still_does_not_override_push_rules() -> None:
    rules = RuleSet()
    policy = _policy(deny_rules=tuple(rules.denies()))

    assert policy.check_intent(_shell("git push origin main")).kind is not None


def test_an_ask_rule_forces_the_prompt_even_with_a_grant() -> None:
    # Not `git push`: push is a hard denial that outranks every rule, so it
    # cannot demonstrate the ask-vs-grant ordering.
    ask = parse_rule("shell(npm install*)", decision=ASK)
    policy = _policy(
        grants=(parse_rule("shell", decision=ALLOW).as_grant(),),
        ask_rules=(ask,),
    )

    decision = policy.check_intent(_shell("npm install left-pad"))

    assert "ask rule" in decision.reason


def test_an_ask_rule_leaves_other_commands_to_the_grant() -> None:
    ask = parse_rule("shell(npm install*)", decision=ASK)
    policy = _policy(
        grants=(parse_rule("shell", decision=ALLOW).as_grant(),),
        ask_rules=(ask,),
    )

    decision = policy.check_intent(_shell("ls -la"))

    assert decision.reason.startswith("scoped grant:")


# ── persistence ─────────────────────────────────────────────────────


def test_rules_round_trip_through_a_file(tmp_path) -> None:
    path = tmp_path / "permissions.yaml"
    save_rule(parse_rule("shell(git *)", decision=ALLOW), path)
    save_rule(parse_rule("shell(rm *)", decision=DENY), path)

    data = path.read_text(encoding="utf-8")

    assert "shell(git *)" in data
    assert "shell(rm *)" in data


def test_a_malformed_stored_rule_is_dropped_not_guessed(tmp_path) -> None:
    path = tmp_path / ".xenon" / "permissions.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"version": 1, "rules": {"allow": ["shell(git *)", "nonsense(x)", 42]}}',
        encoding="utf-8",
    )
    rules = load_rules(workspace=tmp_path)

    assert len(rules.rules) == 1


def test_project_rules_layer_after_user_rules(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".xenon").mkdir(parents=True)
    (home / ".xenon" / "permissions.yaml").write_text(
        '{"version": 1, "rules": {"allow": ["shell"]}}', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "repo"
    (project / ".xenon").mkdir(parents=True)
    (project / ".xenon" / "permissions.yaml").write_text(
        '{"version": 1, "rules": {"deny": ["shell(rm *)"]}}', encoding="utf-8"
    )

    rules = load_rules(workspace=project)

    decision, rule = rules.evaluate(Capability.SHELL, ["rm -rf /"])
    assert decision == DENY
    assert rule.source == "project"


def test_removing_a_rule_by_selector(tmp_path) -> None:
    rules = RuleSet()
    rule = parse_rule("shell(git *)", decision=ALLOW)
    rules.add(rule)

    assert rules.remove("shell(git *)") is True
    assert rules.rules == []


def test_adding_the_same_rule_twice_does_not_duplicate() -> None:
    rules = RuleSet()
    rule = parse_rule("shell(git *)", decision=ALLOW)
    rules.add(rule)
    rules.add(rule)

    assert len(rules.rules) == 1


def test_grants_only_expose_allow_rules() -> None:
    rules = RuleSet()
    rules.add(parse_rule("shell", decision=ALLOW))
    rules.add(parse_rule("verify", decision=DENY))

    assert [grant.capability for grant in rules.grants()] == [Capability.SHELL]


def test_a_rule_payload_round_trips() -> None:
    rule = parse_rule("shell(git *)", decision=ALLOW, source="user")

    restored = PermissionRule.from_payload(rule.payload())

    assert restored == rule
