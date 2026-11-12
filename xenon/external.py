"""Review another agent's work by refusing to take its word for it.

Xenon can verify its own runs because every tool call becomes a receipt in its
own ledger.  Another agent — Claude Code, Codex, anything else — has no such
ledger, and its transcript is written by the party being reviewed.  A tool
result in that log saying a test passed is a claim, not evidence: the agent
could have mis-recorded it, or skipped the run and said it did.

So this module splits the two roles:

* The **transcript** supplies only *claims* — which files this session says it
  changed, which commands it says it ran.  Reading them is safe; they are
  labelled as claims and never counted as evidence.
* The **worktree** supplies *facts* — what is actually different on disk,
  observed by Xenon's own :class:`GitWorktreeProbe`.

A claim that the worktree does not corroborate is reported as exactly that.
That is the whole product: the reviewed agent cannot certify itself.

What this deliberately does *not* do is assert that the work was correct.  A
file being dirty proves an edit reached disk, never that the edit was right.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from xenon.kernel.contracts import PropositionKind

SCHEMA = "xenon.external_audit/v0"

#: Tools whose ``file_path``/``path`` input names a file the agent wrote to.
#: Edit and Write are unambiguous.  ``MultiEdit``/``NotebookEdit`` are the same
#: act spelled differently across Claude Code versions.
_WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "str_replace_editor"})

#: Write-like tools that create the file when it does not exist yet.
_CREATE_TOOLS = frozenset({"Write", "NotebookEdit"})

_EDIT_KEYS = ("file_path", "path", "notebook_path")


@dataclass(frozen=True, slots=True)
class ClaimedChange:
    """One file the transcript says the agent wrote to."""

    path: str
    kind: PropositionKind
    tools: tuple[str, ...]
    calls: int


@dataclass(frozen=True, slots=True)
class ClaimedCommand:
    """One shell command the transcript says the agent ran.

    Only ever a claim: the transcript records what the agent *intended* to run
    and what it says came back.  Nothing here is evidence.
    """

    command: str
    tool: str
    exit_code: int | None = None
    result_is_error: bool = False


@dataclass(frozen=True, slots=True)
class Finding:
    """How one claimed change compares to the worktree."""

    path: str
    verified: bool
    detail: str
    #: False when the claim cannot be checked at all — the path is outside the
    #: audited workspace, or there was no VCS to read.  An uncheckable claim is
    #: not a refuted one; conflating them turns "I cannot tell" into "you lied".
    verifiable: bool = True


@dataclass
class ExternalAudit:
    agent: str
    workspace: str
    session: str = ""
    claimed_changes: tuple[ClaimedChange, ...] = ()
    claimed_commands: tuple[ClaimedCommand, ...] = ()
    findings: tuple[Finding, ...] = ()
    dirty_paths: tuple[str, ...] = ()
    head: str = ""
    #: The commit the session started from.  Empty when it could not be told,
    #: in which case the comparison falls back to uncommitted changes only.
    base: str = ""
    #: Paths the agent changed that no claim accounts for.  Not an accusation:
    #: a formatter, a build, or a prior session can dirty a worktree.
    unclaimed_paths: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)

    @property
    def verified_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.findings if item.verified)

    @property
    def contradicted_paths(self) -> tuple[str, ...]:
        """Claims the workspace actively refutes: it could be checked, and it
        shows no such change."""

        return tuple(
            item.path for item in self.findings if not item.verified and item.verifiable
        )

    @property
    def unverifiable_paths(self) -> tuple[str, ...]:
        """Claims that could not be checked at all (outside workspace / no VCS)."""

        return tuple(item.path for item in self.findings if not item.verifiable)

    @property
    def evidence_status(self) -> str:
        if not self.claimed_changes:
            return "unverified"
        if self.contradicted_paths:
            return "contradicted"
        if self.unverifiable_paths:
            return "partial" if self.verified_paths else "unverified"
        if self.unclaimed_paths:
            return "partial"
        return "verified"


def _normalized(path: str) -> str:
    return path.replace("\\", "/").strip().lower().lstrip("./").rstrip("/")


def _relative_to(path: str, workspace: Path) -> str:
    """Map an absolute path from the transcript onto the workspace.

    Returns the workspace-relative POSIX path, or the original path when it is
    outside the workspace — which is itself worth reporting rather than hiding:
    a change the probe cannot see is a change Xenon cannot vouch for.
    """

    candidate = Path(path)
    try:
        if candidate.is_absolute():
            return candidate.resolve().relative_to(workspace).as_posix()
    except (ValueError, OSError):
        return path
    return path.replace("\\", "/")


def _claim_inside(path: str, workspace: Path) -> bool:
    """Whether the probe could have seen this path at all.

    Adapters map absolute in-workspace paths to relative ones; anything still
    absolute is outside the audited tree, so a missing diff there proves
    nothing about the claim.
    """

    candidate = Path(path)
    if not candidate.is_absolute():
        return True
    try:
        candidate.resolve().relative_to(workspace)
        return True
    except (ValueError, OSError):
        return False


def _iter_tool_uses(path: Path) -> Iterable[Mapping[str, Any]]:
    """Yield ``tool_use`` blocks from a Claude Code transcript.

    The transcript is JSONL with one event per line and a few event types
    (queue opacity, attachments, snapshots) that carry no tool calls.  Parsing
    is line-by-line and forgiving: a truncated final line is normal for a
    session that is still running, and it must not abort the audit.
    """

    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, Mapping) or event.get("type") != "assistant":
                continue
            message = event.get("message")
            content = message.get("content") if isinstance(message, Mapping) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, Mapping) and block.get("type") == "tool_use":
                    yield block


def _iter_tool_results(path: Path) -> dict[str, Mapping[str, Any]]:
    """Index ``tool_result`` blocks by ``tool_use_id``."""

    results: dict[str, Mapping[str, Any]] = {}
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, Mapping) or event.get("type") != "user":
                continue
            message = event.get("message")
            content = message.get("content") if isinstance(message, Mapping) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") != "tool_result":
                    continue
                identifier = str(block.get("tool_use_id") or "")
                if identifier:
                    results[identifier] = block
    return results


def read_claude_code_claims(
    transcript: Path, *, workspace: Path
) -> tuple[tuple[ClaimedChange, ...], tuple[ClaimedCommand, ...]]:
    """Extract what the session *claims* it did.  Claims only, never facts."""

    workspace = workspace.resolve()
    changes: dict[str, dict[str, Any]] = {}
    commands: list[ClaimedCommand] = []

    for block in _iter_tool_uses(transcript):
        name = str(block.get("name") or "")
        payload = block.get("input")
        if not isinstance(payload, Mapping):
            continue

        if name in _WRITE_TOOLS:
            raw = next(
                (payload.get(key) for key in _EDIT_KEYS if payload.get(key)),
                None,
            )
            if not isinstance(raw, str) or not raw.strip():
                continue
            path = _relative_to(raw.strip(), workspace)
            entry = changes.get(_normalized(path))
            if entry is None:
                entry = {
                    "path": path,
                    "kind": (
                        PropositionKind.FILE_CREATED
                        if name in _CREATE_TOOLS
                        else PropositionKind.FILE_CHANGED
                    ),
                    "tools": set(),
                    "calls": 0,
                }
                changes[_normalized(path)] = entry
            entry["tools"].add(name)
            entry["calls"] += 1
            # One real edit outranks any number of "created"s: a file that was
            # edited after being written is a changed file, not a new one.
            if name not in _CREATE_TOOLS:
                entry["kind"] = PropositionKind.FILE_CHANGED

        if name == "Bash":
            command = payload.get("command")
            if isinstance(command, str) and command.strip():
                commands.append(ClaimedCommand(command=command.strip(), tool=name))

    ordered = [
        ClaimedChange(
            path=entry["path"],
            kind=entry["kind"],
            tools=tuple(sorted(entry["tools"])),
            calls=entry["calls"],
        )
        for entry in sorted(changes.values(), key=lambda item: _normalized(item["path"]))
    ]
    return tuple(ordered), tuple(commands)


class ClaimAdapter(Protocol):
    """Turns one transcript format into claims.

    Keyed by *format*, not by vendor: the same agent changes its log shape
    across versions, and two agents can share one.  An adapter may only say
    what the transcript claims — never what actually happened.
    """

    name: str

    def sniff(self, transcript: Path) -> bool:
        """Is this transcript in the format this adapter reads?"""

    def read(
        self, transcript: Path, *, workspace: Path
    ) -> tuple[tuple[ClaimedChange, ...], tuple[ClaimedCommand, ...]]:
        """(claimed changes, claimed commands) — claims only."""


class ClaudeCodeAdapter:
    """Claude Code's JSONL session log."""

    name = "claude-code"
    #: How far into the file sniffing looks before giving up.  A real session
    #: has assistant/user records within the first handful of lines; a file
    #: that does not is not this format, and reading all of it proves nothing.
    _SNIFF_LINES = 200

    def sniff(self, transcript: Path) -> bool:
        try:
            with transcript.open("r", encoding="utf-8", errors="replace") as stream:
                for index, line in enumerate(stream):
                    if index >= self._SNIFF_LINES:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, Mapping):
                        continue
                    if event.get("type") not in ("assistant", "user"):
                        continue
                    message = event.get("message")
                    if isinstance(message, Mapping) and isinstance(
                        message.get("content"), list
                    ):
                        return True
        except OSError:
            return False
        return False

    def read(
        self, transcript: Path, *, workspace: Path
    ) -> tuple[tuple[ClaimedChange, ...], tuple[ClaimedCommand, ...]]:
        return read_claude_code_claims(transcript, workspace=workspace)


#: Every transcript format Xenon can read claims from, in sniffing order.
ADAPTERS: tuple[ClaimAdapter, ...] = (ClaudeCodeAdapter(),)


def resolve_adapter(transcript: Path, agent: str = "") -> ClaimAdapter | None:
    """Pick an adapter by explicit name first, then by format.

    An explicit name that matches nothing returns ``None`` rather than falling
    back to sniffing: the caller asked for a specific parser, and quietly using
    another one would attribute claims to the wrong agent.
    """

    if agent:
        for adapter in ADAPTERS:
            if adapter.name == agent:
                return adapter
        return None
    for adapter in ADAPTERS:
        try:
            if adapter.sniff(transcript):
                return adapter
        except OSError:
            continue
    return None


def _first_timestamp(transcript: Path) -> float | None:
    with transcript.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, Mapping):
                continue
            stamp = event.get("timestamp")
            if not isinstance(stamp, str):
                continue
            try:
                from datetime import datetime

                return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    return None


def _baseline_for(transcript: Path, probe: Any) -> str:
    """The commit the session started from, or "" when it cannot be told.

    Guessing wrong here is not cosmetic: a base newer than the session hides
    the session's own work, a base older credits it with someone else's.  So
    the lookup is exact — the newest commit at or before the first event — and
    an unreadable history leaves the base empty rather than inventing one.
    """

    started = _first_timestamp(transcript)
    if started is None:
        return ""
    lookup = getattr(probe, "commit_before", None)
    if lookup is None:
        return ""
    return str(lookup(started) or "")


def _head_of(probe: Any) -> str:
    reader = getattr(probe, "head", None)
    if callable(reader):
        return str(reader() or "")
    return ""


def audit(
    transcript: Path,
    *,
    workspace: Path,
    probe: Any | None = None,
    base: str | None = None,
    agent: str = "",
) -> ExternalAudit:
    """Compare what the transcript claims against what the worktree shows.

    ``base`` is the commit the session started from.  Reviewing a finished
    session against bare ``git status`` asks the wrong question: an agent that
    committed its work leaves a clean worktree, so every committed file would
    look like an unmet claim.  Without an explicit base the earliest commit
    timestamp is used.

    ``probe`` is any object with the :class:`GitWorktreeProbe` shape.  It is
    injected so the kernel-side code stays VCS-agnostic and tests need no git.

    ``agent`` names an adapter explicitly; otherwise the transcript format
    decides.  A log in a format no adapter recognises yields no claims at all
    and says so in ``notes`` — guessing would attribute invented claims to the
    reviewed agent, which is exactly the failure this module exists to prevent.
    """

    workspace = workspace.resolve()
    adapter = resolve_adapter(transcript, agent)
    claims: tuple[ClaimedChange, ...] = ()
    commands: tuple[ClaimedCommand, ...] = ()
    if adapter is not None:
        claims, commands = adapter.read(transcript, workspace=workspace)
    result = ExternalAudit(
        agent=adapter.name if adapter is not None else (agent or "unknown"),
        workspace=str(workspace),
        session=transcript.stem,
        claimed_changes=claims,
        claimed_commands=commands,
    )
    if adapter is None:
        result.notes.append(
            "无法识别这份对话记录的格式（没有匹配的适配器），未解析任何声称；"
            "如确知来源，可用 --agent 指定解析器。"
        )

    if probe is None:
        from xenon.catalog.worktree import GitWorktreeProbe

        probe = GitWorktreeProbe(workspace)

    resolved_base = base or _baseline_for(transcript, probe)
    if resolved_base:
        result.base = resolved_base
        changed = probe.changed_since(resolved_base)
    else:
        changed = probe.probe()
    if changed is None:
        # No VCS, or git refused.  Every claim stays unproven — and saying so
        # is the honest answer, not a reason to fall back to trusting the log.
        result.notes.append(
            "无法读取工作区状态（没有 VCS 或 git 不可用），所有声明均未被核实。"
        )
        result.findings = tuple(
            Finding(
                path=item.path,
                verified=False,
                detail="工作区无法观测，未能核验",
                verifiable=False,
            )
            for item in claims
        )
        return result

    changed = tuple(getattr(changed, "dirty_paths", changed))
    dirty = {_normalized(path): path for path in changed}
    result.dirty_paths = tuple(changed)
    result.head = _head_of(probe)
    if adapter is None:
        # The worktree facts are real, but nothing can be compared against
        # them: no claim was parsed.  Listing every dirty path as "unclaimed"
        # would read as an accusation against a log we could not read at all.
        return result

    findings: list[Finding] = []
    claimed_normalized: set[str] = set()
    for claim in claims:
        key = _normalized(claim.path)
        claimed_normalized.add(key)
        if not _claim_inside(claim.path, workspace):
            findings.append(
                Finding(
                    path=claim.path,
                    verified=False,
                    detail="不在审计工作区内，无法核验",
                    verifiable=False,
                )
            )
            continue
        if key in dirty:
            findings.append(
                Finding(
                    path=claim.path,
                    verified=True,
                    detail=f"工作区确有改动（{dirty[key]}）",
                )
            )
        else:
            findings.append(
                Finding(
                    path=claim.path,
                    verified=False,
                    detail="日志声称改过，工作区没有对应改动",
                )
            )
    result.findings = tuple(findings)
    result.unclaimed_paths = tuple(
        path for key, path in sorted(dirty.items()) if key not in claimed_normalized
    )
    return result


def to_report(result: ExternalAudit) -> dict[str, Any]:
    """Shape an audit like ``xenon.report`` so the same renderers can print it."""

    def _finding_for(path: str) -> Finding | None:
        key = _normalized(path)
        return next(
            (item for item in result.findings if _normalized(item.path) == key), None
        )

    def _status_of(path: str) -> str:
        finding = _finding_for(path)
        if finding is None:
            return "unresolved"
        if finding.verified:
            return "bound"
        # "I could not check" is not "the workspace says otherwise".
        return "out_of_scope" if not finding.verifiable else "contradicted"

    claims = [
        {
            "id": f"claim-{index}",
            "kind": item.kind.value,
            "target": item.path,
            "status": _status_of(item.path),
            "reason": (
                finding.detail if (finding := _finding_for(item.path)) is not None else ""
            ),
            "fact_ids": [],
        }
        for index, item in enumerate(result.claimed_changes, start=1)
    ]
    contradictions = [
        {
            "kind": "claim_not_bound",
            "claim": claim,
            "detail": claim["reason"],
        }
        for claim in claims
        if claim["status"] == "contradicted"
    ]
    out_of_scope = [
        {
            "kind": "claim_out_of_scope",
            "claim": claim,
            "detail": claim["reason"],
        }
        for claim in claims
        if claim["status"] == "out_of_scope"
    ]

    return {
        "schema": SCHEMA,
        "task": {
            "id": result.session,
            "objective": "",
            "workspace": result.workspace,
            "model": "",
            "mode": "external",
            "constraints": [],
            "obligations": [],
        },
        "status": {
            "phase": "audited",
            "accepted": True,
            "evidence_status": result.evidence_status,
            "answer_verified": result.evidence_status == "verified",
            "delivered": True,
            "commit": result.head,
        },
        "integrity": {
            "valid": True,
            "reason": "",
            "events": 0,
            "last_seq": 0,
            "last_hash": "",
        },
        "claims": claims,
        "verdicts": [],
        "gaps": [
            {
                "id": f"unclaimed-{index}",
                "stage": "v0",
                "kind": "file_changed",
                "target": path,
                "reason": "工作区有改动，但日志没有对应声明",
                "attempts": 0,
            }
            for index, path in enumerate(result.unclaimed_paths, start=1)
        ],
        "delivery": {
            "verified_paths": list(result.verified_paths),
            "unverified_paths": list(result.contradicted_paths),
            "out_of_scope_paths": list(result.unverifiable_paths),
            "receipt_paths": list(result.dirty_paths),
            "hash_mismatches": [],
        },
        "tools": {
            "calls": len(result.claimed_commands),
            "failures": 0,
            "by_tool": {"Bash": len(result.claimed_commands)},
            "tests": [],
        },
        "contradictions": contradictions,
        "out_of_scope": out_of_scope,
        "answer": "",
        "replay": {
            "ledger": result.workspace,
            "command": (
                f"xenon audit-external {result.session}"
                + (f" --base {result.base[:12]}" if result.base else "")
            ),
        },
        "limits": {
            "note": (
                "外部 agent 的日志只提供声明；事实来自 Xenon 自己观测的工作区。"
                "「工作区确有改动」证明改动落到了磁盘，不证明改动是否正确。"
            )
        },
        "notes": list(result.notes),
    }


__all__ = [
    "ClaimedChange",
    "ClaimedCommand",
    "ExternalAudit",
    "Finding",
    "SCHEMA",
    "audit",
    "read_claude_code_claims",
    "render_audit",
    "to_report",
]


_VERDICTS = {
    "verified": "✅ 声明与工作区一致",
    "partial": "⚠️ 部分声明获佐证；其余无法核验，或存在未声明的改动",
    "contradicted": "❌ 声明与工作区冲突",
    "unverified": "⚠️ 没有可核实的声明",
}


def render_audit(result: ExternalAudit, *, comment: bool = False) -> str:
    """Render an external audit in terms that are true for another agent.

    The ledger renderers say "receipts" and "what actually ran", which is right
    for Xenon's own runs and wrong here: nothing in this report is a receipt.
    Every line below distinguishes what the session *said* from what Xenon
    *observed*.
    """

    lines = [
        f"# External Audit — `{result.agent}` · `{result.session[:12]}`",
        "",
        f"**{_VERDICTS.get(result.evidence_status, result.evidence_status)}**",
        "",
        f"**工作区：** `{result.workspace}`"
        + (f" · **基线：** `{result.base[:12]}`" if result.base else " · **基线：** 未确定"),
        f"**日志声明：** 改动 {len(result.claimed_changes)} 个文件 · "
        f"命令 {len(result.claimed_commands)} 条；声明只是声明，不是证据。",
        f"**工作区实测：** 相对基线变动 {len(result.dirty_paths)} 个文件"
        f" · **HEAD** `{(result.head or 'none')[:12]}`",
        "",
    ]

    if result.notes:
        for note in result.notes:
            lines.append(f"> {note}")
        lines.append("")

    corroborated = [item for item in result.findings if item.verified]
    contradicted = [
        item for item in result.findings if not item.verified and item.verifiable
    ]
    unverifiable = [item for item in result.findings if not item.verifiable]

    if corroborated:
        lines.append(f"## 已获工作区佐证（{len(corroborated)}）")
        lines.append("")
        for item in corroborated[:60]:
            lines.append(f"- `{item.path}`")
        if len(corroborated) > 60:
            lines.append(f"- …另 {len(corroborated) - 60} 个")
        lines.append("")

    if contradicted:
        lines.append(f"## 与工作区冲突（{len(contradicted)}）")
        lines.append("")
        lines.append(
            "> 这些路径在工作区可被检查，但相对基线没有对应的改动。"
        )
        lines.append("")
        for item in contradicted:
            lines.append(f"- `{item.path}` — {item.detail}")
        lines.append("")

    if unverifiable:
        lines.append(f"## 无法核验（{len(unverifiable)}）")
        lines.append("")
        lines.append(
            "> 这些声明不在审计工作区内（或工作区没有 VCS），"
            "既不能证实也不能证伪；它们不计入「与工作区冲突」。"
        )
        lines.append("")
        for item in unverifiable:
            lines.append(f"- `{item.path}` — {item.detail}")
        lines.append("")

    if result.unclaimed_paths:
        lines.append(f"## 有改动但无人声明（{len(result.unclaimed_paths)}）")
        lines.append("")
        lines.append(
            "> 这些路径相对基线有变动，但会话日志里没有对应的写入操作。"
            "可能是脚本、格式化工具或另一个会话造成的——这里只陈述事实。"
        )
        lines.append("")
        for path in result.unclaimed_paths[:40]:
            lines.append(f"- `{path}`")
        if len(result.unclaimed_paths) > 40:
            lines.append(f"- …另 {len(result.unclaimed_paths) - 40} 个")
        lines.append("")

    lines += [
        "## 如何复现",
        "",
        f"- 会话日志：`{result.session}`",
        f"- 命令：`{('xenon audit-external ' + result.session) + ((' --base ' + result.base[:12]) if result.base else '')}`",
        "",
        "> 外部 agent 的日志只提供声明；事实来自 Xenon 自己观测的工作区。"
        "「工作区确有改动」证明改动落到了磁盘，不证明改动是否正确。",
        "",
    ]
    if comment:
        # A PR comment must lead with the verdict, so the repetition is
        # deliberate: the body is long and a reviewer reads the top only.
        return "\n".join(lines[:6] + ["", *(lines[6:])])
    return "\n".join(lines)
