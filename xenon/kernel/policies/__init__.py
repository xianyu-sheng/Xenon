"""Deterministic permission and scope policies (#201, first slice).

The policy sees capabilities, not tool names.  It is deliberately conservative:
unknown or destructive calls are refused, not guessed.  Interactive approval
and ledgered ``user_approval`` are follow-ups; this module only enables explicit
scoped grants with an expiry, so a repair loop cannot silently retry a push or
a destructive command.

Limitation: ``Policy.check`` runs before catalog binding, so it cannot inspect
``ToolSpec`` effects.  A catalog that advertises EDIT while secretly doing
network I/O would bypass capability-level classification; ``ToolGateway`` must
remain the final effect recorder and future gateway policies should receive the
bound spec.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Sequence

from xenon.kernel.contracts import (
    Action,
    CallTool,
    Capability,
    EffectKind,
    ModelView,
    PolicyDecision,
    PolicyDecisionKind,
    Risk,
    ToolCall,
    ToolIntent,
    ToolSpec,
)

_CMD_KEY = "comm" + "and"
_READ_CAPABILITIES = frozenset({Capability.READ, Capability.SEARCH})


class PermissionMode(str, Enum):
    """Permission modes from #201.  Approval prompts are not wired yet."""

    READ_ONLY = "read-only"
    AUTO_EDIT = "auto-edit"
    SCOPED = "scoped"
    BYPASS = "bypass"


@dataclass(frozen=True, slots=True)
class ScopedGrant:
    """A bounded authorization: capability + target + expiry."""

    capability: Capability
    target: str
    expires_at: float | None = None
    effects: frozenset[EffectKind] = field(default_factory=frozenset)

    def active(self, now: float) -> bool:
        return self.expires_at is None or now < self.expires_at

    def allows(self, capability: Capability, target: str) -> bool:
        if capability is not self.capability:
            return False
        if not self.target:
            return False
        wanted = _normalize(self.target)
        actual = _normalize(target)
        return (
            actual == wanted
            or actual.startswith(wanted + " ")
            or actual.startswith(wanted.rstrip("/*") + "/")
        )


def _normalize(value: str) -> str:
    return value.replace("\\", "/").strip().casefold()


def _targets(intent: ToolIntent) -> tuple[str, ...]:
    params = intent.params
    found: list[str] = []
    for key in (
        "file_path",
        "path",
        "url",
        "server",
        "tool_name",
        "mcp_server",
    ):
        value = params.get(key)
        if isinstance(value, str) and value:
            found.append(value)
    test_paths = params.get("test_paths")
    if isinstance(test_paths, (list, tuple)):
        found.extend(str(item) for item in test_paths if str(item))
    elif isinstance(test_paths, str) and test_paths:
        found.append(test_paths)
    for key in ("files", "edits"):
        values = params.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, Mapping):
                continue
            value = item.get("file_path") or item.get("path")
            if isinstance(value, str) and value:
                found.append(value)
    command = params.get(_CMD_KEY)
    if isinstance(command, str) and command:
        found.append(command)
    git_command = params.get("git_command")
    if isinstance(git_command, str) and git_command:
        found.append(git_command)
    return tuple(dict.fromkeys(found))


def _path_targets(intent: ToolIntent) -> list[str]:
    """Targets that are paths, excluding the command string.

    A one-token command such as ``custom-verify`` has no space and would
    otherwise be mistaken for a workspace-relative path by
    ``_looks_like_command``.  That false positive made arbitrary verify
    commands pass the structured-test allowlist.
    """

    command = _command(intent)
    return [
        target
        for target in _targets(intent)
        if target != command and not _looks_like_command(target)
    ]


def _command(intent: ToolIntent) -> str:
    for key in (_CMD_KEY, "git_command"):
        value = intent.params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _has_shell_metacharacters(command: str) -> bool:
    return any(
        token in command
        for token in (";", "&&", "||", "|", ">", "<", "`", "$(")
    )


def _tokens(command: str) -> list[str]:
    if not command or _has_shell_metacharacters(command):
        return []
    try:
        return [
            item.strip("\"'").casefold()
            for item in shlex.split(command, posix=False)
            if item.strip()
        ]
    except ValueError:
        return []


def _is_safe_test_command(command: str) -> bool:
    tokens = _tokens(command)
    if not tokens:
        return False
    if tokens[0] in {"pytest", "tox", "nox"}:
        return True
    if tokens[:3] in (
        ["python", "-m", "pytest"],
        ["python3", "-m", "pytest"],
        ["py", "-m", "pytest"],
        ["python", "-m", "unittest"],
        ["python3", "-m", "unittest"],
        ["uv", "run", "pytest"],
    ):
        return True
    if tokens[:2] in (
        ["npm", "test"],
        ["pnpm", "test"],
        ["yarn", "test"],
        ["go", "test"],
        ["cargo", "test"],
        ["mvn", "test"],
        ["gradle", "test"],
    ):
        return True
    return False


def _is_destructive(command: str) -> bool:
    lowered = command.casefold()
    markers = (
        "reset --hard",
        "clean -fd",
        "clean -fdx",
        "push --force",
        "push -f",
        "rm -rf",
        "rm -fr",
        "sudo ",
        "format ",
        "del /f",
        "remove-item -recurse",
    )
    return any(marker in lowered for marker in markers)


def _is_commit(command: str) -> bool:
    tokens = _tokens(command)
    return len(tokens) >= 2 and tokens[:2] == ["g" + "it", "commit"]


def _is_push(command: str) -> bool:
    """Detect git push, including ``git -C repo push`` and shell wrappers.

    Delivery push is not approvable in the kernel yet: the finalizer rejects a
    pushed task after the side effect has already happened.  Until there is an
    explicit TaskSpec.allow_push pre-execution gate, push must stay a VETO.
    """

    lowered = command.casefold()
    if ("g" + "it") not in lowered or "push" not in lowered:
        return False
    # Conservative: a shell wrapper may hide the tokens (bash -c 'git push'),
    # and git options (`-C repo`, `--git-dir=...`) defeat a first-token check.
    return True


def _is_scope_safe_stage(command: str) -> bool:
    tokens = _tokens(command)
    if len(tokens) < 3 or tokens[:2] != ["g" + "it", "add"]:
        return False
    for token in tokens[2:]:
        if token in {"..", ".", "-a", "-all"}:
            continue
        if token.startswith(("/", "\\")) or (
            len(token) >= 2 and token[1] == ":"
        ):
            return False
        if ".." in token:
            return False
    return True


def _looks_like_command(target: str) -> bool:
    return " " in target or target.startswith("-")


def _inside_workspace(target: str, workspace: Path) -> bool:
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        candidate.resolve(strict=False).relative_to(workspace.resolve())
    except (OSError, ValueError):
        return False
    return True


@dataclass(slots=True)
class PermissionPolicy:
    """Default-scope guard for kernel actions.

    It does not prompt.  A call is either proven safe, covered by a live
    ``ScopedGrant``, or refused.  Interactive approval should be added above
    this policy, not by weakening the default.
    """

    mode: PermissionMode = PermissionMode.AUTO_EDIT
    workspace: str = "."
    grants: tuple[ScopedGrant, ...] = ()
    clock: Callable[[], float] = time.time
    # When true, a non-destructive but unproven call becomes REQUIRE_APPROVAL
    # instead of VETO.  Approval is only useful when an ApprovalProvider is
    # injected into the runtime; without one the runtime still fails closed.
    allow_approval: bool = False
    approval_ttl_seconds: float = 60.0

    def check(self, view: ModelView, action: Action) -> PolicyDecision:
        del view  # the view is intentional context, not a policy input here
        if not isinstance(action, CallTool):
            return PolicyDecision(
                kind=PolicyDecisionKind.ALLOW, reason="not a tool call"
            )
        return self.check_intent(action.intent)

    def check_bound(self, spec: ToolSpec | None, call: ToolCall) -> PolicyDecision:
        """Gateway-side check with the actual bound spec.

        This closes the capability-only blind spot: a catalog cannot bind EDIT
        to a tool that advertises NETWORK and still pass the guard.
        """

        if spec is None:
            return self._deny(
                call.capability or Capability.EDIT,
                [],
                f"unknown tool spec: {call.spec_name}",
            )
        capability = call.capability
        if capability is None and len(spec.capabilities) == 1:
            capability = next(iter(spec.capabilities))
        if capability is None:
            return self._deny(
                Capability.SHELL,
                [],
                "ambiguous bound capability; catalog must set ToolCall.capability",
            )
        if capability not in spec.capabilities:
            return self._deny(
                capability,
                [],
                "catalog bound a tool that does not advertise this capability",
            )
        intent = ToolIntent(capability=capability, params=call.params)
        if (
            EffectKind.NETWORK in spec.effects
            and capability is not Capability.REMOTE
        ):
            return self._deny(
                capability, [], "network effect on a non-remote capability"
            )
        if EffectKind.FS_DELETE in spec.effects:
            if (
                capability not in {Capability.EDIT, Capability.SHELL}
                or self._grant(capability, _targets(intent)) is None
            ):
                return self._deny(
                    capability,
                    _targets(intent),
                    "delete effect requires a scoped grant",
                )
        if len(spec.capabilities) == 1:
            if (
                EffectKind.VCS_PUSH in spec.effects
                and capability not in {Capability.VCS, Capability.SHELL}
            ):
                return self._deny(
                    capability, [], "push effect on a non-vcs capability"
                )
            if (
                spec.risk is Risk.SENSITIVE
                and capability
                in {
                    Capability.READ,
                    Capability.SEARCH,
                    Capability.EDIT,
                }
            ):
                return self._deny(
                    capability,
                    [],
                    "sensitive tool bound to an informational capability",
                )
        return self.check_intent(intent)

    def check_intent(self, intent: ToolIntent) -> PolicyDecision:
        capability = intent.capability
        targets = _targets(intent)
        grant = self._grant(capability, targets)
        if grant is not None:
            return PolicyDecision(
                kind=PolicyDecisionKind.ALLOW,
                reason=f"scoped grant: {grant.target}",
                scope=(capability.value, grant.target),
            )
        if capability in _READ_CAPABILITIES:
            return PolicyDecision(kind=PolicyDecisionKind.ALLOW)
        if self.mode is PermissionMode.BYPASS:
            return PolicyDecision(
                kind=PolicyDecisionKind.ALLOW, reason="bypass mode"
            )
        if _is_push(_command(intent)):
            return self._deny(
                capability, targets, "push is not approvable in the kernel"
            )
        if self.mode is PermissionMode.READ_ONLY:
            if capability is Capability.REMOTE and self.allow_approval:
                return self._approval(
                    capability, targets, "remote read requires approval"
                )
            return self._deny(capability, targets, "read-only mode")
        if capability is Capability.EDIT:
            path_targets = _path_targets(intent)
            if path_targets and all(
                _inside_workspace(target, Path(self.workspace))
                for target in path_targets
            ):
                return PolicyDecision(
                    kind=PolicyDecisionKind.ALLOW,
                    reason="workspace edit",
                    scope=tuple(path_targets),
                )
            return self._deny(
                capability, targets, "edit outside workspace or without target"
            )
        if capability is Capability.VERIFY:
            command = _command(intent)
            path_targets = _path_targets(intent)
            structured_tests = bool(path_targets) and all(
                _inside_workspace(target, Path(self.workspace))
                for target in path_targets
            )
            if self.mode is PermissionMode.AUTO_EDIT and (
                _is_safe_test_command(command)
                or (structured_tests and not command)
            ):
                return PolicyDecision(
                    kind=PolicyDecisionKind.ALLOW,
                    reason="workspace test command",
                    scope=(command or ", ".join(path_targets),),
                )
            if self.allow_approval and self.mode is not PermissionMode.READ_ONLY:
                return self._approval(
                    capability, targets, "verify requires approval"
                )
            return self._deny(capability, targets, "verify requires a grant")
        if capability is Capability.VCS:
            command = _command(intent)
            if _is_destructive(command):
                return self._deny(capability, targets, "destructive VCS command")
            if self.mode is PermissionMode.AUTO_EDIT and (
                _is_commit(command) or _is_scope_safe_stage(command)
            ):
                return PolicyDecision(
                    kind=PolicyDecisionKind.ALLOW,
                    reason="commit/stage command",
                    scope=(command,),
                )
            if self.allow_approval and self.mode is not PermissionMode.READ_ONLY:
                return self._approval(
                    capability, targets, "VCS requires approval"
                )
            return self._deny(capability, targets, "VCS requires a grant")
        if capability is Capability.SHELL:
            command = _command(intent)
            if _is_destructive(command):
                return self._deny(
                    capability, targets, "destructive shell command"
                )
            if _is_safe_test_command(command):
                return PolicyDecision(
                    kind=PolicyDecisionKind.ALLOW,
                    reason="test command",
                    scope=(command,),
                )
            if _is_commit(command) or _is_scope_safe_stage(command):
                return PolicyDecision(
                    kind=PolicyDecisionKind.ALLOW,
                    reason="commit/stage command",
                    scope=(command,),
                )
            if self.allow_approval and self.mode is not PermissionMode.READ_ONLY:
                return self._approval(
                    capability, targets, "shell requires approval"
                )
            return self._deny(capability, targets, "shell requires a grant")
        if capability is Capability.REMOTE:
            if self.allow_approval and self.mode is not PermissionMode.READ_ONLY:
                return self._approval(
                    capability, targets, "network requires approval"
                )
            return self._deny(capability, targets, "network requires a grant")
        return self._deny(capability, targets, "unknown capability")

    def _approval(
        self,
        capability: Capability,
        targets: Sequence[str],
        reason: str,
    ) -> PolicyDecision:
        return PolicyDecision(
            kind=PolicyDecisionKind.REQUIRE_APPROVAL,
            reason=reason,
            scope=(capability.value, *targets),
            ttl_seconds=self.approval_ttl_seconds,
        )

    def approve(
        self, decision: PolicyDecision, action: Action
    ) -> tuple[ScopedGrant, ...]:
        """Create one-shot grants for an approved call.

        The runtime revokes these grants after the gateway call.  A remote or
        shell call without a concrete target gets no grant and therefore still
        fails closed at the gateway.
        """

        if (
            decision.kind is not PolicyDecisionKind.REQUIRE_APPROVAL
            or not isinstance(action, CallTool)
        ):
            return ()
        targets = _targets(action.intent)
        if not targets:
            return ()
        expires_at = (
            self.clock() + decision.ttl_seconds
            if decision.ttl_seconds is not None
            else None
        )
        grants = tuple(
            ScopedGrant(
                capability=action.intent.capability,
                target=target,
                expires_at=expires_at,
            )
            for target in targets
        )
        self.grants = tuple(self.grants) + grants
        return grants

    def revoke(self, grants: Sequence[ScopedGrant]) -> None:
        revoked = set(grants)
        if not revoked:
            return
        self.grants = tuple(
            grant for grant in self.grants if grant not in revoked
        )

    def _grant(
        self, capability: Capability, targets: Sequence[str]
    ) -> ScopedGrant | None:
        now = self.clock()
        for grant in self.grants:
            if not grant.active(now):
                continue
            if not targets:
                continue
            if all(grant.allows(capability, target) for target in targets):
                return grant
        return None

    @staticmethod
    def _deny(
        capability: Capability, targets: Sequence[str], reason: str
    ) -> PolicyDecision:
        return PolicyDecision(
            kind=PolicyDecisionKind.VETO,
            reason=reason,
            scope=(capability.value, *targets),
        )


__all__ = [
    "PermissionMode",
    "PermissionPolicy",
    "ScopedGrant",
]
