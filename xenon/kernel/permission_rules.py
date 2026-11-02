"""Pattern-based permission rules, modelled on Claude Code's settings (#373).

Claude Code's model, which this follows:

* rules are ``Tool(pattern)`` — ``Bash(pytest:*)``, ``Read(src/**)``;
* three lists — ``allow`` / ``ask`` / ``deny`` — and **deny beats ask beats
  allow**, so a broad allow can never unlock something someone denied;
* rules live in layered settings (user, project), so a team can share the safe
  ones and an individual can keep their own;
* the prompt offers "don't ask again", which *writes a rule* rather than
  flipping an invisible switch.

Xenon keeps its own semantics on top: a rule is a **recorded human decision**
(``GRANT_ADDED`` / ``RULE_ADDED``), never evidence.  Nothing here touches claims,
verdicts or the evidence status.

Where Xenon deliberately differs: a rule names an **effect capability**, not a
tool name, because the kernel's vocabulary is capabilities and the catalog owns
tool names.  The pattern is matched against the call's targets (path or command).
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from xenon.kernel.contracts import Capability
from xenon.kernel.policies import ScopedGrant

__all__ = [
    "ALLOW",
    "ASK",
    "DENY",
    "PermissionRule",
    "RuleSet",
    "default_rule_paths",
    "parse_rule",
]

ALLOW = "allow"
ASK = "ask"
DENY = "deny"

_DECISIONS = {ALLOW, ASK, DENY}


def _normalize(value: str) -> str:
    return str(value or "").replace("\\", "/").strip().casefold()


def _split_selector(text: str) -> tuple[str, str]:
    """``"shell(git *)"`` -> ``("shell", "git *")``; bare capability -> ``*``."""

    body = text.strip()
    if "(" not in body:
        return body, "*"
    if not body.endswith(")"):
        raise ValueError(f"unbalanced rule selector: {text!r}")
    capability, pattern = body[:-1].split("(", 1)
    return capability.strip(), pattern.strip() or "*"


@dataclass(frozen=True, slots=True)
class PermissionRule:
    """One remembered decision, with the pattern it covers."""

    decision: str
    capability: Capability
    pattern: str = "*"
    source: str = "session"
    reason: str = ""

    def matches(self, capability: Capability, targets: Sequence[str]) -> bool:
        if self.decision not in _DECISIONS:
            return False
        if capability is not self.capability:
            return False
        if self.pattern == "*":
            return True
        if not targets:
            # A rule naming a pattern cannot authorize a call with no target:
            # that would silently broaden it to "anything".
            return False
        return all(
            fnmatch.fnmatchcase(_normalize(target), _normalize(self.pattern))
            for target in targets
        )

    def as_grant(self) -> ScopedGrant:
        return ScopedGrant(capability=self.capability, target=self.pattern)

    def selector(self) -> str:
        return f"{self.capability.value}({self.pattern})"

    def payload(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "capability": self.capability.value,
            "pattern": self.pattern,
            "source": self.source,
            "reason": self.reason,
        }

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> PermissionRule | None:
        try:
            capability = Capability(str(data["capability"]))
        except (KeyError, ValueError):
            return None
        decision = str(data.get("decision") or "").strip().lower()
        if decision not in _DECISIONS:
            return None
        return cls(
            decision=decision,
            capability=capability,
            pattern=str(data.get("pattern") or "*"),
            source=str(data.get("source") or "session"),
            reason=str(data.get("reason") or ""),
        )


def parse_rule(text: str, *, decision: str, source: str = "session") -> PermissionRule:
    """Parse one settings line, e.g. ``deny: "shell(rm *:*)"``."""

    capability, pattern = _split_selector(text)
    try:
        cap = Capability(capability.strip().casefold())
    except ValueError as exc:
        raise ValueError(f"unknown capability in rule {text!r}") from exc
    if decision not in _DECISIONS:
        raise ValueError(f"unknown decision {decision!r}")
    return PermissionRule(
        decision=decision, capability=cap, pattern=pattern, source=source
    )


@dataclass
class RuleSet:
    """Layered rules: every source is loaded, precedence handles conflicts."""

    rules: list[PermissionRule] = field(default_factory=list)

    def add(self, rule: PermissionRule) -> PermissionRule:
        for index, existing in enumerate(self.rules):
            if (
                existing.capability is rule.capability
                and existing.pattern == rule.pattern
                and existing.decision == rule.decision
            ):
                self.rules[index] = rule
                return rule
        self.rules.append(rule)
        return rule

    def remove(self, selector: str) -> bool:
        wanted = _normalize(selector)
        before = len(self.rules)
        self.rules = [
            rule
            for rule in self.rules
            if _normalize(rule.selector()) != wanted
        ]
        return len(self.rules) != before

    def evaluate(
        self, capability: Capability, targets: Sequence[str]
    ) -> tuple[str, PermissionRule] | None:
        """Highest-precedence decision: deny > ask > allow.

        This ordering is the part that must not be relaxed: a broad allow in a
        user file cannot unlock something a project file denies.
        """

        hits = [
            rule for rule in self.rules if rule.matches(capability, targets)
        ]
        if not hits:
            return None
        for decision in (DENY, ASK, ALLOW):
            for rule in hits:
                if rule.decision == decision:
                    return decision, rule
        # Unreachable by construction: ``matches`` rejects an unknown decision,
        # so every hit has one of the three above.  Falling through returns None,
        # which callers already treat as "no rule applies".


    def grants(self) -> tuple[ScopedGrant, ...]:
        """The ``allow`` rules, expressed as policy grants."""

        return tuple(
            rule.as_grant() for rule in self.rules if rule.decision == ALLOW
        )

    def denies(self) -> tuple[PermissionRule, ...]:
        return tuple(rule for rule in self.rules if rule.decision == DENY)

    def rows(self) -> list[dict[str, Any]]:
        return [rule.payload() for rule in self.rules]

    # ── persistence ──────────────────────────────────────────────────

    def merge(self, other: RuleSet) -> None:
        for rule in other.rules:
            self.add(rule)

    def to_document(self) -> dict[str, Any]:
        document: dict[str, list[str]] = {ALLOW: [], ASK: [], DENY: []}
        for rule in self.rules:
            document[rule.decision].append(rule.selector())
        return {"version": 1, "rules": document}

    def load_document(self, data: Any, *, source: str) -> None:
        if not isinstance(data, dict):
            return
        rules = data.get("rules")
        if not isinstance(rules, dict):
            return
        for decision in (ALLOW, ASK, DENY):
            entries = rules.get(decision)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                try:
                    rule = parse_rule(str(entry), decision=decision, source=source)
                except ValueError:
                    # A malformed line must not be guessed at: dropping it only
                    # costs one prompt, while keeping it could authorize.
                    continue
                self.add(rule)


def default_rule_paths(workspace: str | Path | None = None) -> list[tuple[str, Path]]:
    """Layered settings files, lowest precedence first.

    Same shape as Claude Code: user settings, then project settings.  The project
    file is meant to be committed, which is why it is under ``.xenon/``.
    """

    paths: list[tuple[str, Path]] = []
    # ``Path.home()`` rather than ``expanduser("~")``: it is the one spellings a
    # test can patch, and on Windows ``expanduser`` reads USERPROFILE while a
    # test that sets HOME would silently write to the real user directory.
    home = Path.home() / ".xenon" / "permissions.yaml"
    paths.append(("user", home))
    if workspace is not None:
        paths.append(
            ("project", Path(workspace) / ".xenon" / "permissions.yaml")
        )
    return paths


def load_rules(workspace: str | Path | None = None) -> RuleSet:
    """Read every settings file that exists.  Missing files are not errors."""

    rules = RuleSet()
    for source, path in default_rule_paths(workspace):
        data = _read_yaml(path)
        if data is not None:
            rules.load_document(data, source=source)
    return rules


def save_rule(rule: PermissionRule, path: Path) -> None:
    """Append one rule to a settings file, preserving the rest.

    Rewriting the whole document (instead of appending text) keeps the file
    valid even if the user hand-edited it into a different order.
    """

    data = _read_yaml(path) or {"version": 1, "rules": {}}
    rules = data.setdefault("rules", {})
    entries = rules.setdefault(rule.decision, [])
    if not isinstance(entries, list):
        entries = rules[rule.decision] = []
    selector = rule.selector()
    if selector not in entries:
        entries.append(selector)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _read_yaml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 — fall back to JSON, never crash startup
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            return None
    return loaded if isinstance(loaded, dict) else None


def ruleset_from_payload(rows: Iterable[Any]) -> RuleSet:
    rules = RuleSet()
    for row in rows:
        if isinstance(row, dict):
            rule = PermissionRule.from_payload(row)
            if rule is not None:
                rules.add(rule)
    return rules
