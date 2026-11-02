"""Pattern-based permission rules (#373).

The ordering is the safety property: **deny > ask > allow**.  A broad allow in a
user file must never unlock something a project file denies, and an "ask" rule
must outrank a remembered grant.
"""

from __future__ import annotations

from pathlib import Path

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


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never read or write the real user's rule file.

    A test that leaked into `~/.xenon/permissions.yaml` is what made this suite
    order-dependent; isolating home is cheaper than remembering not to.
    """

    home = tmp_path / "_home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


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


# ── remaining branches (patch coverage is a real gate) ──────────────


def test_an_unknown_decision_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_rule("shell", decision="maybe")


def test_matching_rejects_an_invalid_stored_decision() -> None:
    rule = PermissionRule(
        decision="maybe", capability=Capability.SHELL, pattern="*"
    )
    assert rule.matches(Capability.SHELL, ["ls"]) is False


def test_from_payload_needs_a_known_capability() -> None:
    assert PermissionRule.from_payload({"decision": ALLOW, "capability": "nope"}) is None


def test_from_payload_needs_a_known_decision() -> None:
    assert (
        PermissionRule.from_payload({"decision": "maybe", "capability": "shell"})
        is None
    )


def test_evaluate_returns_none_for_an_empty_set() -> None:
    assert RuleSet().evaluate(Capability.SHELL, ["ls"]) is None


def test_rows_and_merge_round_trip() -> None:
    left = RuleSet()
    left.add(parse_rule("shell(git *)", decision=ALLOW))
    right = RuleSet()
    right.add(parse_rule("verify", decision=DENY))

    left.merge(right)

    assert len(left.rows()) == 2
    assert {row["capability"] for row in left.rows()} == {"shell", "verify"}


def test_a_document_holds_all_three_lists() -> None:
    rules = RuleSet()
    rules.add(parse_rule("shell(git *)", decision=ALLOW))
    rules.add(parse_rule("shell(rm *)", decision=DENY))
    rules.add(parse_rule("shell(npm i*)", decision=ASK))

    document = rules.to_document()

    assert document["rules"][ALLOW] == ["shell(git *)"]
    assert document["rules"][ASK] == ["shell(npm i*)"]
    assert document["rules"][DENY] == ["shell(rm *)"]


def test_loading_a_document_with_wrong_shapes_is_ignored() -> None:
    rules = RuleSet()
    rules.load_document("nonsense", source="user")
    rules.load_document({"rules": []}, source="user")
    rules.load_document({"rules": {"allow": "shell"}}, source="user")

    assert rules.rules == []


def test_rewriting_a_file_keeps_existing_entries(tmp_path) -> None:
    """``save_rule`` must not clobber a hand-edited file."""

    path = tmp_path / "permissions.yaml"
    path.write_text(
        '{"version": 1, "rules": {"allow": ["shell(git *)"]}}', encoding="utf-8"
    )
    save_rule(parse_rule("verify", decision=DENY), path)
    data = path.read_text(encoding="utf-8")

    assert "shell(git *)" in data and "verify" in data


def test_saving_the_same_rule_twice_writes_one_entry(tmp_path) -> None:
    path = tmp_path / "permissions.yaml"
    rule = parse_rule("shell(git *)", decision=ALLOW)
    save_rule(rule, path)
    save_rule(rule, path)

    assert path.read_text(encoding="utf-8").count("shell(git *)") == 1


def test_saving_into_a_non_list_bucket_replaces_it(tmp_path) -> None:
    path = tmp_path / "permissions.yaml"
    path.write_text(
        '{"version": 1, "rules": {"allow": "not-a-list"}}', encoding="utf-8"
    )

    save_rule(parse_rule("shell(git *)", decision=ALLOW), path)

    data = path.read_text(encoding="utf-8")
    assert "shell(git *)" in data


def test_a_yaml_file_is_read_as_yaml(tmp_path) -> None:
    path = tmp_path / ".xenon" / "permissions.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "version: 1\nrules:\n  deny:\n    - shell(rm *)\n", encoding="utf-8"
    )

    rules = load_rules(workspace=tmp_path)

    assert rules.evaluate(Capability.SHELL, ["rm -rf /"])[0] == DENY


def test_an_unreadable_file_is_not_fatal(tmp_path, monkeypatch) -> None:
    directory = tmp_path / ".xenon"
    directory.mkdir()
    (directory / "permissions.yaml").mkdir()  # a directory, not a file

    rules = load_rules(workspace=tmp_path)

    assert rules.rules == []


def test_a_non_mapping_document_is_ignored(tmp_path) -> None:
    path = tmp_path / ".xenon" / "permissions.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("- just\n- a list\n", encoding="utf-8")

    assert load_rules(workspace=tmp_path).rules == []


def test_ruleset_from_payload_skips_garbage() -> None:
    from xenon.kernel.permission_rules import ruleset_from_payload

    rules = ruleset_from_payload(
        [{"decision": ALLOW, "capability": "shell", "pattern": "*"}, "x", {}]
    )

    assert len(rules.rules) == 1


def test_an_unreadable_file_falls_back_to_nothing(tmp_path, monkeypatch) -> None:
    """A permission file that cannot be read must not crash startup."""

    from pathlib import Path as _Path

    path = tmp_path / ".xenon" / "permissions.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("rules: {}", encoding="utf-8")
    monkeypatch.setattr(
        _Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(OSError("x"))
    )

    assert load_rules(workspace=tmp_path).rules == []


def test_invalid_yaml_falls_back_to_json_then_gives_up(tmp_path) -> None:
    path = tmp_path / ".xenon" / "permissions.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("{ this is : not valid yaml [", encoding="utf-8")

    assert load_rules(workspace=tmp_path).rules == []
