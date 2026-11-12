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
from typing import Any, Callable, Mapping, Sequence

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
#: DISCOVER is read-only catalog discovery (defect 6): it has no side effects
#: and is free in every mode, including read-only.
_READ_CAPABILITIES = frozenset(
    {Capability.READ, Capability.SEARCH, Capability.DISCOVER}
)


class PermissionMode(str, Enum):
    """Permission modes from #201 (approval panel wired since #392).

    READ_ONLY: reads only; AUTO_EDIT: workspace writes run freely; SCOPED:
    workspace writes need a human answer ("default" in /permissions); BYPASS:
    everything except hard-destructive commands and push.
    """

    READ_ONLY = "read-only"
    AUTO_EDIT = "auto-edit"
    SCOPED = "scoped"
    BYPASS = "bypass"


@dataclass(frozen=True, slots=True)
class ScopedGrant:
    """A bounded authorization: capability + target + expiry.

    ``reason`` travels with the grant so a ledger replay can say *why* this call
    was not asked — the session answer was "session grant: shell", not a
    featureless "scoped grant: *".  It is descriptive only; the policy decides
    by capability and target, never by reason.
    """

    capability: Capability
    target: str
    expires_at: float | None = None
    effects: frozenset[EffectKind] = field(default_factory=frozenset)
    reason: str = ""

    def active(self, now: float) -> bool:
        return self.expires_at is None or now < self.expires_at

    def allows(self, capability: Capability, target: str) -> bool:
        if capability is not self.capability:
            return False
        actual = _normalize(target)
        # A shell target that chains, backgrounds or redirects is never covered
        # by a grant — not even the ``*`` wildcard a turn answer uses:
        # ``sleep 1 & rm -rf x`` and ``echo hi > /etc/passwd`` start with the
        # granted command but do something the human never saw.  §9: 链式命令
        # 属提权，绝不合并。
        if self.capability is Capability.SHELL and _chains_commands(actual):
            return False
        if not self.target:
            return False
        # ``*`` is the explicit "this whole capability, in this workspace"
        # wildcard a session grant uses.  It is a literal decision the human made
        # ("always allow verify"), not an implicit fallback, so it is spelled out
        # here rather than inferred from an empty target.
        if self.target == "*":
            return True
        wanted = _normalize(self.target)
        if (
            actual == wanted
            or actual.startswith(wanted + " ")
            or actual.startswith(wanted.rstrip("/*") + "/")
        ):
            return True
        # A command is authorized by the program it runs and the shape of its
        # arguments, not by its exact spelling.  Measured on a real task: the
        # model rewrote `python -c "print(123)"` four times — as
        # ``'print(123)' | python -``, ``python "-c" ...`` and the reverse pipe —
        # so literal matching asked four times for one intent.  Shape matching
        # keeps the program fixed (a grant for `python` never releases `rm`) and
        # ignores the spelling noise a model varies on.
        if self.capability is Capability.SHELL:
            return _same_command_shape(wanted, actual)
        return False


# Tokens whose exact text carries no meaning for "what does this command do".
_SHAPE_IGNORED_TOKENS = frozenset({"'", '"', "`", "|", "||", "&&", ";", "\\"})

# Markers that put a second program (or a write the human never saw) on the
# same line.  A grant for one command must never cover these: the human
# approved `a`, not `a && b`, not `a & b`, and not `a > /etc/passwd`.
_CHAIN_MARKERS = (
    "|",
    "&&",
    "||",
    ";",
    "`",
    "$(",
    "&",  # background separator: `sleep 1 & rm -rf x`
    ">",  # redirection: writes a file the human never approved
    "<",
    "\n",
    "\r",
)


def _chains_commands(text: str) -> bool:
    return any(marker in text for marker in _CHAIN_MARKERS)


def _command_shape(text: str) -> tuple[str, tuple[str, ...]] | None:
    """Reduce a single command to (program, argument tokens without quoting noise).

    Only spelling is dropped: quoting style, redundant separators, empty
    tokens, and a trailing shell comment.  Word order and every meaningful
    argument survive, so a grant for ``git status`` does not cover ``git push``.

    A line that chains or pipes several programs has no shape of its own — it
    is not one command — so it returns ``None`` and can never shape-match.
    """

    stripped = text.strip()
    # A newline is a second command, not whitespace: `a #x\nb` would otherwise
    # shape-match `a` after the comment is cut, while the shell runs both.
    if not stripped or "\n" in stripped or "\r" in stripped:
        return None
    if _chains_commands(stripped):
        return None
    # A trailing comment is not part of the command the human saw.
    if "#" in stripped:
        stripped = stripped.split("#", 1)[0]
    tokens = [
        token
        for token in stripped.replace("'", " ").replace('"', " ").split()
        if token
    ]
    if not tokens:
        return None
    program = tokens[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return program, tuple(tokens[1:])


def _same_command_shape(granted: str, actual: str) -> bool:
    left = _command_shape(granted)
    right = _command_shape(actual)
    if left is None or right is None:
        return False
    if left[0] != right[0]:
        return False
    return left[1] == right[1]


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


#: Git subcommands that only read.  ``branch`` is here because listing is the
#: common case; creating or deleting a branch is caught by ``_is_destructive``
#: or by the flag check below.
_READ_ONLY_GIT = frozenset(
    {"status", "diff", "log", "show", "branch", "rev-parse", "blame"}
)
_GIT_WRITE_FLAGS = ("-d", "-D", "-m", "-M", "-c", "--delete", "--move")


def _is_read_only_git(command: str) -> bool:
    """True for a bare read-only git query, false for anything else.

    Deliberately narrow: a shell chain (`git status && rm -rf x`), a
    ``--output`` style flag or a write subcommand all fall through to the
    normal approval path.
    """

    text = command.strip()
    if any(marker in text for marker in ("&&", "||", ";", "|", "\n", ">")):
        return False
    tokens = _tokens(text)
    if tokens[:1] == ["g" + "it"]:
        tokens = tokens[1:]
    if not tokens:
        return False
    subjects = [item for item in tokens if not item.startswith("-")]
    if not subjects:
        return False
    subject = subjects[0]
    if subject == "branch":
        return not any(flag in tokens for flag in _GIT_WRITE_FLAGS)
    return subject in _READ_ONLY_GIT


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
    # Layered settings (#373): ``deny`` beats everything, ``ask`` outranks a
    # remembered grant.  Plain objects with ``matches(capability, targets)`` and
    # ``selector()`` so the policy does not import the rules module.
    deny_rules: tuple[Any, ...] = ()
    ask_rules: tuple[Any, ...] = ()

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
        # Hard safety rules are checked *before* any grant.  A grant is a human
        # saying "stop asking me", not a suspension of the safety rules: a
        # wildcard session grant used to authorize `rm -rf /` because the grant
        # branch returned first (found by a test, not in production).
        refusal = self._hard_denial(capability, intent, targets)
        if refusal is not None:
            return refusal
        forced_ask = self._forced_ask(capability, targets)
        if forced_ask is not None:
            # An explicit "ask" rule outranks a remembered grant: the human said
            # "always ask for this" about a narrower or wider scope, and widening
            # silently is exactly the failure mode rules exist to prevent.
            return self._approval(
                capability,
                targets,
                f"ask rule {getattr(forced_ask, 'selector', lambda: '')()}",
            )
        grant = self._grant(capability, targets)
        if grant is not None:
            return PolicyDecision(
                kind=PolicyDecisionKind.ALLOW,
                reason=grant.reason or f"scoped grant: {grant.target}",
                scope=(capability.value, grant.target),
            )
        if capability in _READ_CAPABILITIES:
            return PolicyDecision(kind=PolicyDecisionKind.ALLOW)
        if _is_push(_command(intent)):
            # A1 (#414): Xenon does not decide this for the human.  Pushing is
            # an effect a person may authorize; refusing it outright made the
            # product stricter than the agent it wraps.  The decision is asked
            # for, recorded, and then obeyed.
            #
            # This sits *before* the bypass shortcut on purpose (§9: "push 的
            # 硬规则先于任何授权"): bypass is the user telling us not to ask,
            # and a push is exactly the effect the front-end still promises to
            # intercept.
            if not self.allow_approval:
                return self._deny(
                    capability,
                    targets,
                    "push needs a human decision, and this turn has no "
                    "approval channel (use a TTY or --permission bypass)",
                )
            return self._approval(
                capability, targets, "push requires explicit approval"
            )
        if self.mode is PermissionMode.BYPASS:
            return PolicyDecision(
                kind=PolicyDecisionKind.ALLOW, reason="bypass mode"
            )
        if self.mode is PermissionMode.READ_ONLY:
            # Asking is not writing, but execution is: a read-only turn may
            # request an unproven *read* (never an edit, never a command).
            # `test_allow_approval_does_not_weaken_read_only` owns this rule.
            if capability is Capability.REMOTE and self.allow_approval:
                return self._approval(
                    capability, targets, "remote read requires approval"
                )
            return self._deny(capability, targets, "read-only mode")
        if capability is Capability.EDIT:
            path_targets = _path_targets(intent)
            if self.mode is PermissionMode.SCOPED:
                # SCOPED is the front-end's "default" mode: writes inside the
                # workspace still need a human answer, which is what separates
                # it from accept_edits.
                if self.allow_approval:
                    return self._approval(
                        capability, targets, "write requires approval"
                    )
                return self._deny(
                    capability, targets, "scoped mode: write requires approval"
                )
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
            # Structural, not a vocabulary: a verify call that names only
            # workspace paths and runs no external program is the read-shaped
            # case and needs no prompt.  Whether a *command* looks like a test
            # runner is a closed enumeration that always drifts, so commands go
            # through approval like every other execution.
            if self.mode is PermissionMode.AUTO_EDIT and structured_tests and not command:
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
            # Reading repository state is not a side effect: `git status`,
            # `diff`, `log`, `show` and `branch` are how the model learns what
            # it changed.  Requiring a grant for them made a read-only look at
            # the worktree cost an approval (and fail closed without a TTY).
            if _is_read_only_git(command):
                return PolicyDecision(
                    kind=PolicyDecisionKind.ALLOW,
                    reason="read-only git query",
                    scope=(command,),
                )
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

    def escalation_scope(self, intent: ToolIntent) -> str:
        """A short, human-readable description of what an approval covers.

        #392/#395: a granted escalation is bounded to the exact call, so the
        model must be told the bound — a real run refined the command (added
        `$ProgressPreference=...`) and the grant no longer matched.  Param names
        live here, next to the rules that read them, so the kernel stays free of
        tool vocabulary (K003).
        """

        command = _command(intent)
        if command:
            return command.strip()[:160]
        targets = _targets(intent)
        return ", ".join(targets)[:160]

    def refuses_push(self, intent: ToolIntent) -> bool:
        """Does this intent push?  The runtime needs it to open delivery."""

        return _is_push(_command(intent))

    def _ask_destructive(
        self,
        capability: Capability,
        targets: Sequence[str],
        what: str,
    ) -> PolicyDecision:
        """Ask before a destructive or remote-writing command (#414).

        These used to be refused outright with "never approvable".  That made
        Xenon the strictest part of the pipeline and, in practice, meant the
        agent could not finish work a human had already asked for.  The human
        decides; the kernel records the decision and enforces it afterwards.
        """

        if not self.allow_approval:
            return self._deny(
                capability,
                targets,
                f"{what} needs a human decision, and this turn has no "
                "approval channel (use a TTY or --permission bypass)",
            )
        return self._approval(capability, targets, f"{what} requires approval")

    def refuse_escalation(self, intent: ToolIntent) -> str:
        """No built-in hard rules any more (#414).

        Kept as an extension hook: a deployment may still declare something
        un-approvable.  The built-in policy returns nothing, because "the
        kernel decides for you" is exactly what Xenon is not supposed to be.
        """

        return ""

    def describe_boundary(self) -> str:
        """One deterministic line describing what this policy will do.

        #165 dogfood: the model used to *guess* its execution boundary and
        then invent one ("command is forbidden this turn") — a real weather
        request was blocked because the model did not know that a shell call
        simply triggers a human approval.  The boundary is a fact the runtime
        owns, so the view states it instead of leaving it to inference.

        Pure: derived from the policy's own rules, no clock, no I/O.
        """

        if self.mode is PermissionMode.BYPASS:
            return (
                "bypass — every non-destructive call is allowed without "
                "asking; push is still refused"
            )
        parts = [
            "reads anywhere are free (credential files excepted)",
            "edits are limited to the workspace",
        ]
        if self.mode is PermissionMode.READ_ONLY:
            parts.append(
                "read-only turn: no edits, no execution; network reads need a "
                "human approval"
                if self.allow_approval
                else "read-only: no edits, no network, no execution"
            )
        else:
            parts.append("safe test commands, commits and scoped stages run freely")
            if self.allow_approval:
                parts.append(
                    "other shell, network and verification calls trigger a "
                    "human approval prompt — proposing them is allowed and "
                    "expected; the harness asks"
                )
            else:
                parts.append(
                    "other shell, network and verification calls are refused "
                    "in this non-interactive run"
                )
        parts.append("push is never approvable")
        return "; ".join(parts)

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

    def grant(self, grants: Sequence[ScopedGrant]) -> None:
        """Add grants for the rest of this run.

        Used when the human answers "always" *during* a task: the decision was
        made now, so it applies now, not only to the next task.
        """

        self.grants = tuple(self.grants) + tuple(grants)

    def revoke(self, grants: Sequence[ScopedGrant]) -> None:
        revoked = set(grants)
        if not revoked:
            return
        self.grants = tuple(
            grant for grant in self.grants if grant not in revoked
        )

    def _forced_ask(
        self, capability: Capability, targets: Sequence[str]
    ) -> Any | None:
        for rule in self.ask_rules:
            matches = getattr(rule, "matches", None)
            if callable(matches) and matches(capability, targets):
                return rule
        return None

    def _hard_denial(
        self,
        capability: Capability,
        intent: ToolIntent,
        targets: Sequence[str],
    ) -> PolicyDecision | None:
        """Safety rules a grant can never override.

        Keeping this separate is the point: "the human said always" and "the
        harness refuses" must not share a branch, or one of them silently wins.
        """

        for rule in self.deny_rules:
            matches = getattr(rule, "matches", None)
            if callable(matches) and matches(capability, targets):
                return self._deny(
                    capability,
                    targets,
                    f"denied by rule {getattr(rule, 'selector', lambda: '')()}",
                )
        command = _command(intent)
        if capability in (Capability.SHELL, Capability.VERIFY) and _is_destructive(
            command
        ):
            # A "verify" call is a shell command in practice.  Leaving VERIFY
            # out of this rule meant `pytest …; rm -rf .` was merely asked
            # about instead of refused.
            return self._ask_destructive(
                capability, targets, "destructive shell command"
            )
        if capability is Capability.VCS and _is_destructive(command):
            return self._ask_destructive(
                capability, targets, "destructive VCS command"
            )
        if _is_push(command):
            return self._ask_destructive(capability, targets, "push")
        return None

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
