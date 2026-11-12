"""Child runtimes: one loop, one ledger per child (#204, minimal slice).

A child task is not a second execution model.  It is the same ``run_task`` loop
with its own ledger, its own objective and no delivery commit — so a parent can
delegate work without sharing mutable task state.

Isolation is a *process*, not a thread.  A thread cannot be killed: it was
observed writing files after the parent had already recorded a timeout, which
makes the parent's delivery claim unreliable.  A process group can be killed,
and the kill is observable in the outcome.

Isolation is available (``isolate=True``, on by default for parallel batches):
two children sharing one workspace erased each other's edits on a real run, so
each child gets its own git worktree and returns its work as a patch the parent
applies deliberately.

What this slice deliberately does **not** do: kernel-level spawning (a `spawn`
capability), streaming the child's events into the parent ledger, projecting the
child's facts into the parent's audit, or nested children.  Depth is structurally
1: the child runs the same loop, and the loop has no spawn tool.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from xenon.harness.llm import ModelConfig, build_llm_loop_strategy
from xenon.harness.runner import DEFAULT_MAX_STEPS, run_task
from xenon.kernel.contracts import PropositionKind
from xenon.kernel.policies import PermissionMode
from xenon.kernel.strategies import LoopProfile
from xenon.ledger.events import EventKind, EventSource

logger = logging.getLogger(__name__)

# Child profiles are a subset of the mode catalog: a child works on one
# delegated objective, so it never enables plan approval or review loops.
CHILD_PROFILES: dict[str, LoopProfile] = {
    "react": LoopProfile(),
    "plan-execute": LoopProfile(plan=True, step_scope=True, follow_plan=True),
    "plan-react": LoopProfile(plan=True, step_scope=True, step_calls=6),
    "kernel": LoopProfile(plan=True, step_scope=True, step_calls=6),
    "reflection": LoopProfile(reflect_rounds=1, reflect_calls=1),
}

MAX_CHILD_DEPTH = 1


@dataclass(frozen=True, slots=True)
class ChildOutcome:
    """One child run, as the parent records it."""

    child_id: str
    objective: str
    profile: str
    accepted: bool
    phase: str
    answer: str
    ledger_path: str
    debt: tuple[str, ...]
    tool_calls: int
    elapsed_seconds: float
    error: str | None = None
    # Process identity, so a caller can prove the child is gone (or kill it).
    pid: int = 0
    killed: bool = False
    # Isolation: a child in its own worktree returns its work as a patch, and
    # never touches the shared workspace.
    batch_id: str = ""
    isolated: bool = False
    isolation_note: str = ""
    patch_path: str = ""
    patch_bytes: int = 0

    @property
    def delivered(self) -> bool:
        """A child delivered only if the kernel said so."""

        return self.accepted and self.phase == "finalized" and self.error is None

    def summary(self, *, limit: int = 400) -> str:
        head = f"[{self.child_id}] {self.phase}"
        if self.error:
            return f"{head} · 失败: {self.error}"
        body = self.answer.strip()[:limit] or "（无最终回答）"
        tail = f" · debt: {'; '.join(self.debt)}" if self.debt else ""
        return f"{head}{tail}\n{body}\nledger: {self.ledger_path}"


_PACKAGE_ROOT = Path(__file__).resolve().parents[2]

# Narrowest first: a child may only be *less* privileged than its parent.
_PERMISSION_ORDER = (
    PermissionMode.READ_ONLY,
    PermissionMode.AUTO_EDIT,
    PermissionMode.SCOPED,
    PermissionMode.BYPASS,
)


def _check_permission_subset(
    child: PermissionMode, parent: PermissionMode | None
) -> None:
    """A child must never be able to escalate.

    Without this, ``run_child_task(permission=BYPASS)`` from a read-only session
    would hand the child more authority than the human ever granted.
    """

    if parent is None:
        return
    if _PERMISSION_ORDER.index(child) > _PERMISSION_ORDER.index(parent):
        raise ValueError(
            f"child permission {child.value!r} exceeds parent "
            f"{parent.value!r}; a child cannot escalate"
        )


def child_permission_for(parent: PermissionMode | None) -> PermissionMode:
    """The mode a delegated child runs at: read-only, and never above its parent.

    Delegation is read-only by default (architecture doc §5: "子代理默认只读
    ``min(parent, READ_ONLY)``").  The front-end used to pass through the
    ``run_child_task`` default (``AUTO_EDIT``) while computing the parent from a
    field that does not exist (``repl._execution_level``), so every ``/agent``
    raised "child permission 'auto-edit' exceeds parent 'read-only'" and no
    sub-task could start.
    """

    if parent is None:
        return PermissionMode.READ_ONLY
    return min(
        parent, PermissionMode.READ_ONLY, key=_PERMISSION_ORDER.index
    )


def _child_spec(
    *,
    child_id: str,
    objective: str,
    workspace: str | Path,
    model: ModelConfig,
    profile: str,
    ledger_dir: Path,
    permission: PermissionMode,
    max_steps: int,
    parent_task_id: str = "",
    batch_id: str = "",
    broker_dir: str = "",
    session_grants: Sequence[Any] = (),
) -> dict[str, Any]:
    from xenon.harness.child_worker import model_to_spec

    return {
        "child_id": child_id,
        "objective": objective,
        "workspace": str(workspace),
        "profile": profile,
        "permission": permission.value,
        "max_steps": max_steps,
        "ledger_dir": str(ledger_dir),
        "model": model_to_spec(model),
        "parent_task_id": parent_task_id,
        "batch_id": batch_id,
        "broker_dir": broker_dir,
        "session_grants": [
            _grant_to_payload(grant)
            for grant in session_grants
            if grant is not None
        ],
    }


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child *and anything it spawned*.

    A child that started its own commands must not outlive the kill: those
    grandchildren are exactly the ones that would keep writing to the workspace.
    """

    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _read_outcome(out_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read a child's outcome file.  A corrupt file is an error, never a pass."""

    if not out_path.exists():
        return None, None
    try:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"unreadable child outcome: {exc}"
    if not isinstance(payload, dict):
        return None, "child outcome is not an object"
    return payload, None


def _run_in_subprocess(
    spec: dict[str, Any],
    *,
    ledger_dir: Path,
    timeout: float | None,
    child_id: str,
    approval: Any = None,
    broker_dir: Path | None = None,
) -> tuple[dict[str, Any] | None, str | None, int, bool]:
    """Run one child in its own process group.  Returns (payload, error, pid, killed)."""

    ledger_dir.mkdir(parents=True, exist_ok=True)
    handle = tempfile.mkdtemp(prefix=f"{child_id}-", dir=str(ledger_dir))
    spec_path = Path(handle) / "spec.json"
    out_path = Path(handle) / "outcome.json"
    err_path = Path(handle) / "stderr.log"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    with open(err_path, "wb") as err:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "xenon.harness.child_worker",
                str(spec_path),
                str(out_path),
            ],
            cwd=str(_PACKAGE_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=err,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
        killed = _wait_servicing(proc, timeout=timeout, approval=approval, broker_dir=broker_dir)

    payload, error = _read_outcome(out_path)
    if killed:
        error = f"timeout after {timeout:.0f}s; process group killed"
    elif payload is None and error is None:
        tail = ""
        if err_path.exists():
            tail = err_path.read_text(encoding="utf-8", errors="replace")[-400:].strip()
        error = f"child exited {proc.returncode} without an outcome"
        if tail:
            error = f"{error}: {tail}"
    return payload, error, proc.pid, killed


def _wait_servicing(
    proc: subprocess.Popen,
    *,
    timeout: float | None,
    approval: Any,
    broker_dir: Path | None,
) -> bool:
    """Wait for the child, answering its approval requests.  True if killed.

    Polling instead of ``proc.wait(timeout)`` is not for precision: it is the
    only way the parent can answer the child's requests while the child is still
    running.  The answers come from the parent's own provider, on the parent's
    own thread, so the terminal is never touched by two threads.
    """

    if broker_dir is None:
        try:
            proc.wait(timeout=timeout)
            return False
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            return True

    from xenon.harness.child_broker import serve_requests

    handled: set[str] = set()
    deadline = None if timeout is None else time.time() + timeout
    while proc.poll() is None:
        serve_requests(broker_dir, approval, seen=handled)
        if deadline is not None and time.time() >= deadline:
            _kill_tree(proc)
            return True
        time.sleep(0.05)
    # One last sweep: a request written just before exit deserves an answer.
    serve_requests(broker_dir, approval, seen=handled)
    return False


def _broker_dir(directory: Path, child_id: str) -> Path:
    """A stable per-child exchange directory (created on demand)."""

    path = directory / f"{child_id}.broker"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── parent-side delegation record (section 7.1) ──────────────────────────
#
# Who can observe that a child was killed?  Only the parent.  A killed child
# has no chance to write, and a record written by an untrusted party cannot be
# evidence.  So the *parent* writes the lifecycle into the *parent's* ledger,
# and the child's own ledger stays a separate artifact the parent points at.
#
# Three steps hold the property "unfinished is always recorded":
#
# 1. ``child_started`` before the spawn -- the anchor saying "I expected this
#    to end";
# 2. ``child_finished`` or ``child_interrupted`` after it, the latter also
#    opening a debt so the interruption enters the audit's field of view
#    instead of being silently ignored;
# 3. an unmatched ``child_started`` means the *parent* died; a later start
#    repairs it with ``child_interrupted``/``unknown``.
#
# The parent holds the clock and the child does not, so a child cannot evade
# the timeout by stalling or by claiming a different time.

_TIMEOUT_REASON = "timeout"
_ERROR_REASON = "error"
_UNKNOWN_REASON = "unknown"


def parent_ledger_path(
    ledger_dir: str | Path | None,
    parent_task_id: str,
    parent_ledger: str | Path | None = None,
) -> Path | None:
    """Where the parent's own ledger lives, or ``None`` when unknown.

    ``parent_ledger`` (the path the front-end actually wrote this parent
    task to) wins: the REPL keeps kernel ledgers under ``ledger/repl`` while
    child records live under ``ledger/children``, so guessing
    ``<ledger_dir>/<task_id>.jsonl`` silently pointed at a file that never
    exists and every child lifecycle event was dropped.  The directory
    fallback mirrors ``runner._runtime_bundle`` for callers that do not know
    the full path.
    """

    if parent_ledger:
        return Path(parent_ledger).expanduser()
    if not parent_task_id:
        return None
    directory = (
        Path(ledger_dir).expanduser()
        if ledger_dir is not None
        else Path.home() / ".xenon" / "ledger"
    )
    return directory / f"{parent_task_id}.jsonl"


def _open_parent_ledger(
    ledger_dir: str | Path | None,
    parent_task_id: str,
    parent_ledger: str | Path | None = None,
):
    """Open the parent ledger for appending, or ``None`` if it does not exist.

    Never creates one: a parent that has no ledger has no run to attach to, and
    inventing a file would fabricate a run that never happened.
    """

    from xenon.ledger.store import open_shared

    path = parent_ledger_path(ledger_dir, parent_task_id, parent_ledger)
    if path is None or not path.exists():
        return None
    try:
        return open_shared(path, parent_task_id)
    except Exception:  # noqa: BLE001 — a bad parent ledger must not break delegation
        logger.warning("cannot open parent ledger %s", path, exc_info=True)
        return None


def _interrupt_reason(outcome: ChildOutcome) -> str:
    """Classify why the child did not finish, from what the parent observed.

    ``timeout`` and ``error`` are different facts.  The first is our own clock
    expiring while the child was still working -- the work may be half done.
    The second is the child dying on its own.  Collapsing them into one word
    would make a replay unable to tell "we gave up on it" from "it broke".
    """

    if outcome.killed:
        return _TIMEOUT_REASON
    if outcome.error:
        return _ERROR_REASON
    return _UNKNOWN_REASON


def _seal_child_branch(child_id: str, child_ledger: str | Path | None) -> dict[str, Any]:
    """What the parent observed about the child's chain, at the moment it ended.

    The parent verifies the child's ledger itself before writing the seal.  A
    missing or invalid chain is recorded as ``sealed: false`` with a null head
    -- writing a head the parent never read would be a fabricated fact.
    """

    if not child_ledger:
        return {
            "task_id": child_id,
            "ledger": "",
            "head_seq": 0,
            "head_hash": None,
            "event_count": 0,
            "sealed": False,
            "detail": "child ledger path unknown",
        }
    path = Path(child_ledger)
    if not path.exists():
        return {
            "task_id": child_id,
            "ledger": str(path),
            "head_seq": 0,
            "head_hash": None,
            "event_count": 0,
            "sealed": False,
            "detail": "child ledger not found",
        }
    try:
        from xenon.ledger.store import Ledger

        child = Ledger.open(path)
        report = child.verify()
        return {
            "task_id": child_id,
            "ledger": str(path),
            "head_seq": child.watermark(),
            "head_hash": child.last_hash() or None,
            "event_count": len(child.read()),
            "sealed": bool(report.valid),
            "detail": "" if report.valid else report.reason[:200],
        }
    except Exception as exc:  # noqa: BLE001 — sealing must not break delegation
        return {
            "task_id": child_id,
            "ledger": str(path),
            "head_seq": 0,
            "head_hash": None,
            "event_count": 0,
            "sealed": False,
            "detail": str(exc)[:200],
        }


def record_child_started(
    ledger_dir: str | Path | None,
    parent_task_id: str,
    *,
    child_id: str,
    objective: str,
    workspace: str | Path,
    timeout: float | None,
    batch_id: str = "",
    parent_ledger: str | Path | None = None,
    child_ledger: str | Path | None = None,
) -> bool:
    """Write the expectation anchor before the child exists.

    Written *before* the spawn on purpose: a crash between spawn and write
    would leave a child running with no record of it, and that is exactly the
    "disappeared but nobody knows" case this design forbids.
    """

    ledger = _open_parent_ledger(ledger_dir, parent_task_id, parent_ledger)
    if ledger is None:
        return False
    try:
        ledger.append(
            EventKind.CHILD_STARTED,
            EventSource.RUNTIME,
            {
                "child_id": child_id,
                "objective": objective[:500],
                "workspace": str(workspace),
                # The contract the parent will hold the child to.  A timeout of
                # ``None`` is "no deadline", stated rather than omitted so a
                # replay can tell it from "the field was forgotten".
                "timeout_seconds": None if timeout is None else float(timeout),
                "batch_id": batch_id,
                # The branch point: the parent's own chain was here when it
                # promised to watch this child.  The child's ledger is a side
                # chain; the parent never splices its events into its own.
                "branch": {
                    "task_id": child_id,
                    "ledger": str(child_ledger or ""),
                    "base_seq": ledger.watermark(),
                    "base_hash": ledger.last_hash(),
                    "depth": 1,
                },
            },
        )
        return True
    except Exception:  # noqa: BLE001 — recording must not break the delegation
        logger.warning("cannot record child_started for %s", child_id, exc_info=True)
        return False


def record_child_outcome(
    ledger_dir: str | Path | None,
    parent_task_id: str,
    outcome: ChildOutcome,
    *,
    parent_ledger: str | Path | None = None,
) -> bool:
    """Write the child's terminal state, plus a debt when it did not finish."""

    ledger = _open_parent_ledger(ledger_dir, parent_task_id, parent_ledger)
    if ledger is None:
        return False
    payload = {
        "child_id": outcome.child_id,
        "objective": outcome.objective[:500],
        "phase": outcome.phase,
        "accepted": outcome.accepted,
        "tool_calls": outcome.tool_calls,
        "elapsed_seconds": outcome.elapsed_seconds,
        "ledger_path": outcome.ledger_path,
        "batch_id": outcome.batch_id,
        # The seal: the parent read and verified the child's chain before
        # claiming anything about it.  A later tamper is caught by recomputing
        # the child chain and comparing this head.
        "branch": _seal_child_branch(outcome.child_id, outcome.ledger_path),
    }
    try:
        if outcome.delivered:
            ledger.append(
                EventKind.CHILD_FINISHED,
                EventSource.RUNTIME,
                {**payload, "answer": outcome.answer},
            )
            return True
        reason = _interrupt_reason(outcome)
        ledger.append(
            EventKind.CHILD_INTERRUPTED,
            EventSource.RUNTIME,
            {
                **payload,
                "reason": reason,
                "error": outcome.error or "",
                "pid": outcome.pid,
            },
        )
        # The debt is what puts the interruption in front of the audit.  A
        # reason string in a log nobody reads is not a record of an unfinished
        # task; a debt is, because the loop has to answer for it.
        ledger.append(
            EventKind.DEBT_OPENED,
            EventSource.RUNTIME,
            {
                "obligation_id": f"child:{outcome.child_id}",
                "kind": PropositionKind.REQUIREMENT_MET.value,
                "target": outcome.objective[:200],
                "reason": f"delegated child did not finish ({reason})",
            },
        )
        return True
    except Exception:  # noqa: BLE001
        logger.warning(
            "cannot record outcome for %s", outcome.child_id, exc_info=True
        )
        return False


def _child_ledger_path(
    ledger_dir: str | Path | None,
    child_id: str,
    started_payload: dict[str, Any],
) -> Path:
    """Where the child's own ledger lives, from the start record or the default."""

    branch = started_payload.get("branch")
    if isinstance(branch, dict) and branch.get("ledger"):
        return Path(str(branch["ledger"]))
    directory = (
        Path(ledger_dir)
        if ledger_dir is not None
        else Path.home() / ".xenon" / "ledger" / "children"
    )
    return directory / f"{child_id}.jsonl"


def reconcile_child_records(
    ledger_dir: str | Path | None,
    parent_task_id: str,
    *,
    parent_ledger: str | Path | None = None,
) -> list[str]:
    """Repair delegations a crashed parent left unfinished.

    An unmatched ``child_started`` cannot mean "still running": the parent is
    the only writer, and it is running *now*, so its own previous process is
    gone.  What it cannot tell from the ledger alone is whether the child is
    also gone -- so the repair records exactly that uncertainty as
    ``unknown`` rather than guessing a cause.  Making up "timeout" here would
    be a fabricated fact about a run nobody watched.
    """

    ledger = _open_parent_ledger(ledger_dir, parent_task_id, parent_ledger)
    if ledger is None:
        return []
    started: dict[str, dict[str, Any]] = {}
    closed: set[str] = set()
    for event in ledger.read():
        payload = event.payload if isinstance(event.payload, dict) else {}
        child_id = str(payload.get("child_id") or "")
        if not child_id:
            continue
        if event.kind is EventKind.CHILD_STARTED:
            started[child_id] = payload
        elif event.kind in (EventKind.CHILD_FINISHED, EventKind.CHILD_INTERRUPTED):
            closed.add(child_id)
    repaired: list[str] = []
    for child_id, payload in started.items():
        if child_id in closed:
            continue
        try:
            ledger.append(
                EventKind.CHILD_INTERRUPTED,
                EventSource.RUNTIME,
                {
                    "child_id": child_id,
                    "objective": str(payload.get("objective") or ""),
                    "workspace": str(payload.get("workspace") or ""),
                    "reason": _UNKNOWN_REASON,
                    "repaired": True,
                    "branch": _seal_child_branch(
                        child_id,
                        _child_ledger_path(ledger_dir, child_id, payload),
                    ),
                },
            )
            ledger.append(
                EventKind.DEBT_OPENED,
                EventSource.RUNTIME,
                {
                    "obligation_id": f"child:{child_id}",
                    "kind": PropositionKind.REQUIREMENT_MET.value,
                    "target": str(payload.get("objective") or "")[:200],
                    "reason": (
                        "delegated child has no terminal record; the parent "
                        "process ended before it finished"
                    ),
                },
            )
        except Exception:  # noqa: BLE001
            logger.warning("cannot repair %s", child_id, exc_info=True)
            continue
        repaired.append(child_id)
    return repaired


def _prepare_isolation(
    isolate: bool | None, workspace: str | Path, child_id: str
) -> tuple[Any, str]:
    """Decide whether this child gets its own worktree.

    ``None`` means "caller did not ask": isolation stays off so a single
    sequential child keeps today's behaviour.  When isolation *is* asked for and
    cannot be provided, the run still happens — but the outcome says so, because
    silently running unisolated would be the exact lie this issue is about.
    """

    if not isolate:
        return None, ""
    from xenon.harness.child_worktree import ChildWorktree, WorktreeUnavailable

    try:
        return ChildWorktree.create(workspace, child_id), ""
    except WorktreeUnavailable as exc:
        return None, f"not isolated: {exc}"


def _collect_patch(
    tree: Any, directory: Path, child_id: str
) -> tuple[str, str]:
    """Write the child's work as a patch next to its ledger."""

    if tree is None:
        return "", ""
    try:
        patch = tree.patch()
    except Exception as exc:  # noqa: BLE001 — a lost patch must be reported
        logger.warning("could not collect child patch", exc_info=True)
        return "", f"patch failed: {exc}"
    if not patch.strip():
        return "", ""
    path = directory / f"{child_id}.patch"
    path.write_text(patch, encoding="utf-8")
    return patch, str(path)


def _to_scoped(grant: Any) -> tuple[Any, ...]:
    """Normalize a grant to the kernel's ``ScopedGrant`` shape(s).

    The session book hands out ``SessionGrant``, whose ``as_scoped()`` yields one
    ``ScopedGrant`` per target (a grant covers concrete execution paths — design
    doc §9).  Callers may also hand out a raw ``ScopedGrant``.  The kernel policy
    understands only the latter, so normalization happens once, at the child
    entry, instead of at every call site.
    """

    as_scoped = getattr(grant, "as_scoped", None)
    if callable(as_scoped):
        scoped = as_scoped()
        return tuple(scoped) if isinstance(scoped, tuple) else (scoped,)
    return (grant,)


def _grant_to_payload(grant: Any) -> dict[str, Any]:
    """Serialize one grant for the child spec (kernel shape, not session shape)."""

    capability = getattr(grant, "capability", None)
    return {
        "capability": getattr(capability, "value", str(capability)),
        "target": str(
            getattr(grant, "target", None) or getattr(grant, "scope", None) or ""
        ),
        "expires_at": getattr(grant, "expires_at", None),
        "effects": sorted(
            getattr(effect, "value", str(effect))
            for effect in (getattr(grant, "effects", None) or ())
        ),
        "reason": getattr(grant, "reason", "") or "",
    }


def _grant_from_payload(raw: Any) -> Any | None:
    """Rebuild a grant from the spec, dropping anything unusable (fail-closed)."""

    if not isinstance(raw, dict):
        return None
    from xenon.kernel.contracts import Capability, EffectKind
    from xenon.kernel.policies import ScopedGrant

    try:
        capability = Capability(str(raw["capability"]))
    except (KeyError, ValueError):
        return None
    target = str(raw.get("target") or "")
    if not target:
        return None
    effects: set[EffectKind] = set()
    for value in raw.get("effects") or ():
        try:
            effects.add(EffectKind(str(value)))
        except ValueError:
            continue
    return ScopedGrant(
        capability=capability,
        target=target,
        expires_at=raw.get("expires_at"),
        effects=frozenset(effects),
        reason=str(raw.get("reason") or ""),
    )


def run_child_task(
    *,
    objective: str,
    workspace: str | Path,
    model: ModelConfig,
    profile: str = "kernel",
    parent_task_id: str = "",
    batch_id: str = "",
    ledger_dir: str | Path | None = None,
    permission: PermissionMode = PermissionMode.AUTO_EDIT,
    parent_permission: PermissionMode | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    timeout: float | None = None,
    client: Any = None,
    gateway_context: Any = None,
    approval: Any = None,
    isolate: bool | None = None,
    index: int = 1,
    session_grants: Sequence[Any] = (),
    parent_ledger: str | Path | None = None,
) -> ChildOutcome:
    """Run one delegated task on the loop, in its own ledger.

    Default path is a child **process** in its own process group, so a timeout
    can actually kill it.  ``client``/``gateway_context`` are injection seams for
    tests: they cannot cross a process boundary, so passing either keeps the run
    in this process (and then a timeout can only abandon, not kill).

    ``approval`` is the parent's provider.  It is never used to grant the child
    more than the parent has: ``parent_permission`` (when given) bounds what the
    child may run at all, and every individual request still goes to the parent.

    ``isolate`` puts the child in its own git worktree and returns its work as a
    patch instead of mutating the shared workspace.  Two children editing the
    same file used to erase each other's work; there is no static target set to
    compare for a natural-language objective, so only isolation is honest.

    ``session_grants`` (#373) are the parent's remembered decisions, already
    filtered to the parent's workspace.  They let the child inherit what the
    human already allowed without a round trip through the broker — the child
    still cannot create new grants, because its approval path never writes to
    the parent's book.
    """

    if profile not in CHILD_PROFILES:
        raise KeyError(f"unknown child profile: {profile}")
    _check_permission_subset(permission, parent_permission)
    # One shape downstream: the kernel policy only reads ScopedGrant, so any
    # SessionGrant the parent handed in becomes ScopedGrant here (#373).  One
    # grant may expand to several, because §9 scopes a grant per execution path.
    session_grants = tuple(
        scoped for grant in session_grants for scoped in _to_scoped(grant)
    )
    child_id = f"{parent_task_id or 'child'}-sub{index}-{uuid.uuid4().hex[:6]}"
    # A lone child is still a batch of one: without it, "three dispatched
    # together" and "three dispatched separately" look identical in the ledger.
    batch_id = batch_id or f"{parent_task_id or 'child'}-batch-{uuid.uuid4().hex[:6]}"
    directory = (
        Path(ledger_dir)
        if ledger_dir is not None
        else Path.home() / ".xenon" / "ledger" / "children"
    )
    started = time.time()
    in_process = client is not None or gateway_context is not None

    tree, isolation_note = _prepare_isolation(isolate, workspace, child_id)
    run_workspace = tree.path if tree is not None else workspace
    # The anchor goes down before the spawn: a crash between the two would
    # otherwise leave a child running with no record that anyone expected it.
    record_child_started(
        ledger_dir,
        parent_task_id,
        child_id=child_id,
        objective=objective,
        workspace=run_workspace,
        timeout=timeout,
        batch_id=batch_id,
        parent_ledger=parent_ledger,
        child_ledger=directory / f"{child_id}.jsonl",
    )
    try:
        payload, error, pid, killed = (
            _run_in_process(
                objective=objective,
                workspace=run_workspace,
                model=model,
                profile=profile,
                child_id=child_id,
                parent_task_id=parent_task_id,
                batch_id=batch_id,
                directory=directory,
                permission=permission,
                max_steps=max_steps,
                timeout=timeout,
                client=client,
                gateway_context=gateway_context,
                approval=approval,
                session_grants=session_grants,
            )
            if in_process
            else _run_in_subprocess(
                _child_spec(
                    child_id=child_id,
                    objective=objective,
                    workspace=run_workspace,
                    model=model,
                    profile=profile,
                    ledger_dir=directory,
                    permission=permission,
                    max_steps=max_steps,
                    parent_task_id=parent_task_id,
                    batch_id=batch_id,
                    broker_dir=str(_broker_dir(directory, child_id)),
                    session_grants=session_grants,
                ),
                ledger_dir=directory,
                timeout=timeout,
                child_id=child_id,
                approval=approval,
                broker_dir=_broker_dir(directory, child_id),
            )
        )
        patch, patch_path = _collect_patch(tree, directory, child_id)
    finally:
        if tree is not None:
            tree.cleanup()

    elapsed = round(time.time() - started, 3)
    outcome = (
        ChildOutcome(
            child_id=child_id,
            objective=objective,
            profile=profile,
            accepted=False,
            phase="killed" if killed else "failed",
            answer="",
            ledger_path=str(directory / f"{child_id}.jsonl"),
            debt=(),
            tool_calls=0,
            elapsed_seconds=elapsed,
            error=error or "child did not run",
            pid=pid,
            killed=killed,
            batch_id=batch_id,
            isolated=tree is not None,
            isolation_note=isolation_note,
            patch_path=patch_path,
            patch_bytes=len(patch.encode("utf-8")),
        )
        if payload is None
        else ChildOutcome(
            child_id=child_id,
            objective=objective,
            profile=profile,
            accepted=bool(payload.get("accepted")),
            phase=str(payload.get("phase") or "failed"),
            answer=str(payload.get("answer") or ""),
            ledger_path=str(
                payload.get("ledger_path") or directory / f"{child_id}.jsonl"
            ),
            debt=tuple(payload.get("debt") or ()),
            tool_calls=int(payload.get("tool_calls") or 0),
            elapsed_seconds=elapsed,
            error=error,
            pid=pid,
            killed=killed,
            batch_id=batch_id,
            isolated=tree is not None,
            isolation_note=isolation_note,
            patch_path=patch_path,
            patch_bytes=len(patch.encode("utf-8")),
        )
    )
    # The parent writes the terminal state, including the failure case: the
    # child cannot be trusted to report its own death, and a child that never
    # reported is exactly the case that must not vanish.
    record_child_outcome(
        ledger_dir, parent_task_id, outcome, parent_ledger=parent_ledger
    )
    return outcome


def _run_in_process(
    *,
    objective: str,
    workspace: str | Path,
    model: ModelConfig,
    profile: str,
    child_id: str,
    parent_task_id: str,
    batch_id: str,
    directory: Path,
    permission: PermissionMode,
    max_steps: int,
    timeout: float | None,
    client: Any,
    gateway_context: Any,
    approval: Any = None,
    session_grants: Sequence[Any] = (),
) -> tuple[dict[str, Any] | None, str | None, int, bool]:
    """Test seam: the same loop, in this process.  Cannot kill, only abandon."""

    strategy = build_llm_loop_strategy(model, CHILD_PROFILES[profile], client=client)

    def _run() -> Any:
        return run_task(
            objective,
            user_objective=objective,
            strategy=strategy,
            workspace=workspace,
            ledger_dir=directory,
            task_id=child_id,
            permission=permission,
            # The parent (or the user) owns delivery; a child must not commit.
            finalize=False,
            max_steps=max_steps,
            gateway_context=gateway_context,
            # In-process: the parent's provider is directly reachable, so no
            # file broker is needed.
            approval=approval,
            allow_approval=approval is not None,
            parent_task_id=parent_task_id,
            batch_id=batch_id,
            session_grants=tuple(session_grants),
        )

    run = None
    error: str | None = None
    killed = False
    if timeout is None:
        try:
            run = _run()
        except Exception as exc:  # noqa: BLE001 — a child failure is data
            error = f"{type(exc).__name__}: {exc}"
    else:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(_run)
        try:
            run = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # Honest wording: an in-process child cannot be killed, only
            # abandoned, and it may still be writing.  ``killed`` stays False so
            # a caller cannot mistake this for the isolated path.
            error = (
                f"timeout after {timeout:.0f}s "
                "(in-process seam: thread abandoned, not killed)"
            )
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        finally:
            pool.shutdown(wait=False)
    if run is None:
        return None, error or "child did not run", 0, killed
    result = run.result
    return (
        {
            "child_id": child_id,
            "accepted": result.accepted,
            "phase": result.phase.value,
            "answer": result.answer,
            "ledger_path": str(run.ledger_path),
            "debt": [item.reason for item in result.debt],
            "tool_calls": int(result.metrics.get("tool_calls", 0.0)),
        },
        error,
        0,
        killed,
    )


def run_child_tasks(
    *,
    objectives: Sequence[str],
    workspace: str | Path,
    model: ModelConfig,
    profile: str = "kernel",
    parent_task_id: str = "",
    ledger_dir: str | Path | None = None,
    permission: PermissionMode = PermissionMode.AUTO_EDIT,
    max_steps: int = DEFAULT_MAX_STEPS,
    timeout: float | None = None,
    client: Any = None,
    approval: Any = None,
    parent_permission: PermissionMode | None = None,
    isolate: bool | None = True,
    batch_id: str = "",
    max_workers: int = 4,
    session_grants: Sequence[Any] = (),
    parent_ledger: str | Path | None = None,
) -> tuple[ChildOutcome, ...]:
    """Run delegated tasks in parallel, preserving input order.

    Isolation defaults to **on** here: parallel children share a workspace
    otherwise, and a real run showed two of them silently erasing each other's
    edits.  A caller that wants the old shared-workspace behaviour passes
    ``isolate=False`` explicitly.
    """

    objectives = tuple(objective for objective in objectives if objective.strip())
    if not objectives:
        return ()
    # One dispatch, one batch id: this is what distinguishes "three tasks sent
    # together" from "three tasks sent one after another".
    batch_id = batch_id or f"{parent_task_id or 'child'}-batch-{uuid.uuid4().hex[:6]}"
    workers = max(1, min(max_workers, len(objectives)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                run_child_task,
                objective=objective,
                workspace=workspace,
                model=model,
                profile=profile,
                parent_task_id=parent_task_id,
                batch_id=batch_id,
                ledger_dir=ledger_dir,
                permission=permission,
                max_steps=max_steps,
                timeout=timeout,
                client=client,
                approval=approval,
                parent_permission=parent_permission,
                isolate=isolate,
                index=index,
                session_grants=session_grants,
                parent_ledger=parent_ledger,
            )
            for index, objective in enumerate(objectives, 1)
        ]
        return tuple(future.result() for future in futures)


def child_evidence(outcome: ChildOutcome) -> dict[str, Any]:
    """Session-event payload for one child run (pointer, never a copy)."""

    return {
        "child_id": outcome.child_id,
        "objective": outcome.objective[:200],
        "profile": outcome.profile,
        "accepted": outcome.accepted,
        "phase": outcome.phase,
        "delivered": outcome.delivered,
        "ledger": outcome.ledger_path,
        "debt": list(outcome.debt),
        "tool_calls": outcome.tool_calls,
        "elapsed_seconds": outcome.elapsed_seconds,
        "error": outcome.error,
        "pid": outcome.pid,
        "killed": outcome.killed,
        "batch_id": outcome.batch_id,
        "isolated": outcome.isolated,
        "isolation_note": outcome.isolation_note,
        "patch": outcome.patch_path,
        "patch_bytes": outcome.patch_bytes,
    }


__all__ = [
    "CHILD_PROFILES",
    "MAX_CHILD_DEPTH",
    "ChildOutcome",
    "child_evidence",
    "child_permission_for",
    "parent_ledger_path",
    "record_child_outcome",
    "record_child_started",
    "reconcile_child_records",
    "run_child_task",
    "run_child_tasks",
]
