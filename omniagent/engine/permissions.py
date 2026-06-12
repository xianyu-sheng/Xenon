"""Tool permission policies for OmniAgent.

The first permission layer is deliberately non-interactive: tools can be
allowed or denied from built-in defaults plus an optional local
``.omniagent/policy.yaml`` file. Interactive approval can later sit on top of
the same decision model.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_POLICY_PATH = Path(".omniagent") / "policy.yaml"
VALID_DECISIONS = {"allow", "deny", "ask"}


@dataclass(frozen=True)
class PermissionResult:
    decision: str
    reason: str
    matched: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


@dataclass
class ToolPolicy:
    default: str = "allow"
    allow_patterns: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)
    sensitive: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolPolicy":
        default = str(data.get("default", "allow")).lower()
        if default not in VALID_DECISIONS:
            default = "allow"
        return cls(
            default=default,
            allow_patterns=[str(p) for p in data.get("allow_patterns", []) or []],
            deny_patterns=[str(p) for p in data.get("deny_patterns", []) or []],
            sensitive=bool(data.get("sensitive", False)),
        )


DEFAULT_TOOL_POLICIES: dict[str, ToolPolicy] = {
    "command": ToolPolicy(
        default="allow",
        deny_patterns=[
            r"rm\s+(-[rfR]+\s+)?/",
            r"rm\s+(-[rfR]+\s+)?~",
            r"rmdir\s+/",
            r"del\s+/[sfq]\s+[a-zA-Z]:\\",
            r"del\s+/[sfq]\s+C:\\",
            r"\bformat\s+[a-zA-Z]:",
            r"\bmkfs\b",
            r"\bdd\s+if=",
            r"\bshutdown\b",
            r"\breboot\b",
            r"\bhalt\b",
            r"curl.*\|\s*(?:bash|sh|python|node)",
            r"wget.*\|\s*(?:bash|sh|python|node)",
            r"Remove-Item\s+-[rR].*C:\\",
            r"Format-Volume",
            r"Clear-RecycleBin\s+-Force",
            r"\bchmod\s+777\b",
            r"\bchown\b.*root",
        ],
        sensitive=True,
    ),
    "git": ToolPolicy(
        default="allow",
        deny_patterns=[
            r"push\s+--force",
            r"push\s+-f",
            r"reset\s+--hard",
            r"clean\s+-fd",
            r"clean\s+-fXd",
            r"checkout\s+--\s+\.",
            r"branch\s+-D",
            r"reflog\s+expire\s+--all",
        ],
        sensitive=True,
    ),
    "write_file": ToolPolicy(default="allow", sensitive=True),
    "edit_file": ToolPolicy(default="allow", sensitive=True),
    "batch_write": ToolPolicy(default="allow", sensitive=True),
    "batch_edit": ToolPolicy(default="allow", sensitive=True),
    "create_directory": ToolPolicy(default="allow", sensitive=True),
    "mcp_call": ToolPolicy(default="allow", sensitive=True),
}


def _merge_policy(base: ToolPolicy, override: ToolPolicy) -> ToolPolicy:
    return ToolPolicy(
        default=override.default or base.default,
        allow_patterns=base.allow_patterns + override.allow_patterns,
        deny_patterns=base.deny_patterns + override.deny_patterns,
        sensitive=base.sensitive or override.sensitive,
    )


class PermissionManager:
    """Evaluate tool calls against static permission policies."""

    def __init__(
        self,
        policies: dict[str, ToolPolicy] | None = None,
        *,
        policy_path: Path | str | None = None,
    ) -> None:
        self.policy_path = Path(policy_path) if policy_path is not None else DEFAULT_POLICY_PATH
        self.policies: dict[str, ToolPolicy] = dict(DEFAULT_TOOL_POLICIES)
        if policies:
            for name, policy in policies.items():
                self.policies[name] = _merge_policy(self.policies.get(name, ToolPolicy()), policy)
        self._load_local_policy()

    def _load_local_policy(self) -> None:
        if not self.policy_path.exists():
            return
        try:
            data = yaml.safe_load(self.policy_path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.warning("failed to load permission policy %s: %s", self.policy_path, e)
            return

        tools = data.get("tools", data)
        if not isinstance(tools, dict):
            return
        for name, raw in tools.items():
            if not isinstance(raw, dict):
                continue
            override = ToolPolicy.from_dict(raw)
            self.policies[str(name)] = _merge_policy(self.policies.get(str(name), ToolPolicy()), override)

    def evaluate(self, tool_name: str, params: dict[str, Any] | None = None) -> PermissionResult:
        params = params or {}
        policy = self.policies.get(tool_name, ToolPolicy())
        target = self._target_text(tool_name, params)

        for pattern in policy.deny_patterns:
            if self._matches(pattern, target):
                return PermissionResult("deny", f"matched deny pattern for {tool_name}", pattern)

        for pattern in policy.allow_patterns:
            if self._matches(pattern, target):
                return PermissionResult("allow", f"matched allow pattern for {tool_name}", pattern)

        if policy.default == "deny":
            return PermissionResult("deny", f"default deny for {tool_name}")
        if policy.default == "ask":
            return PermissionResult("ask", f"approval required for {tool_name}")
        return PermissionResult("allow", f"default allow for {tool_name}")

    @staticmethod
    def _matches(pattern: str, target: str) -> bool:
        try:
            if re.search(pattern, target, flags=re.IGNORECASE):
                return True
        except re.error:
            pass
        return fnmatch.fnmatch(target.lower(), pattern.lower())

    @staticmethod
    def _target_text(tool_name: str, params: dict[str, Any]) -> str:
        if tool_name == "command":
            return str(params.get("command") or params.get("action") or "")
        if tool_name == "git":
            return str(params.get("git_command") or params.get("command") or "")
        if tool_name in {"write_file", "edit_file", "create_directory"}:
            return str(params.get("file_path") or params.get("path") or "")
        if tool_name in {"batch_write", "batch_edit"}:
            return json.dumps(params, ensure_ascii=False, sort_keys=True)
        if tool_name == "mcp_call":
            return json.dumps(
                {
                    "tool_name": params.get("tool_name", ""),
                    "tool_args": params.get("tool_args", {}),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        return json.dumps(params, ensure_ascii=False, sort_keys=True)


_DEFAULT_MANAGER: PermissionManager | None = None


def get_permission_manager(policy_path: Path | str | None = None) -> PermissionManager:
    global _DEFAULT_MANAGER
    if policy_path is not None:
        return PermissionManager(policy_path=policy_path)
    if _DEFAULT_MANAGER is None:
        _DEFAULT_MANAGER = PermissionManager()
    return _DEFAULT_MANAGER


def reset_permission_manager() -> None:
    """Clear the cached default manager.

    Useful after editing ``.omniagent/policy.yaml`` in a long-lived process or
    in tests that isolate their working directory.
    """
    global _DEFAULT_MANAGER
    _DEFAULT_MANAGER = None
