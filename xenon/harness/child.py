"""Child runtimes: one loop, one ledger per child (#204, minimal slice).

A child task is not a second execution model.  It is the same ``run_task`` loop
with its own ledger, its own objective and no delivery commit — so a parent can
delegate work without sharing mutable task state.

Isolation is a *process*, not a thread.  A thread cannot be killed: it was
observed writing files after the parent had already recorded a timeout, which
makes the parent's delivery claim unreliable.  A process group can be killed,
and the kill is observable in the outcome.

What this slice deliberately does **not** do: kernel-level spawning (a `spawn`
capability), streaming the child's events into the parent ledger, projecting the
child's facts into the parent's audit, or nested children.  Depth is structurally
1: the child runs the same loop, and the loop has no spawn tool.
"""

from __future__ import annotations

import concurrent.futures
import json
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
from xenon.harness.runner import run_task
from xenon.kernel.policies import PermissionMode
from xenon.kernel.strategies import LoopProfile

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
    broker_dir: str = "",
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
        "broker_dir": broker_dir,
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


def run_child_task(
    *,
    objective: str,
    workspace: str | Path,
    model: ModelConfig,
    profile: str = "kernel",
    parent_task_id: str = "",
    ledger_dir: str | Path | None = None,
    permission: PermissionMode = PermissionMode.AUTO_EDIT,
    parent_permission: PermissionMode | None = None,
    max_steps: int = 20,
    timeout: float | None = None,
    client: Any = None,
    gateway_context: Any = None,
    approval: Any = None,
    index: int = 1,
) -> ChildOutcome:
    """Run one delegated task on the loop, in its own ledger.

    Default path is a child **process** in its own process group, so a timeout
    can actually kill it.  ``client``/``gateway_context`` are injection seams for
    tests: they cannot cross a process boundary, so passing either keeps the run
    in this process (and then a timeout can only abandon, not kill).

    ``approval`` is the parent's provider.  It is never used to grant the child
    more than the parent has: ``parent_permission`` (when given) bounds what the
    child may run at all, and every individual request still goes to the parent.
    """

    if profile not in CHILD_PROFILES:
        raise KeyError(f"unknown child profile: {profile}")
    _check_permission_subset(permission, parent_permission)
    child_id = f"{parent_task_id or 'child'}-sub{index}-{uuid.uuid4().hex[:6]}"
    directory = (
        Path(ledger_dir)
        if ledger_dir is not None
        else Path.home() / ".xenon" / "ledger" / "children"
    )
    started = time.time()
    in_process = client is not None or gateway_context is not None

    if in_process:
        payload, error, pid, killed = _run_in_process(
            objective=objective,
            workspace=workspace,
            model=model,
            profile=profile,
            child_id=child_id,
            parent_task_id=parent_task_id,
            directory=directory,
            permission=permission,
            max_steps=max_steps,
            timeout=timeout,
            client=client,
            gateway_context=gateway_context,
            approval=approval,
        )
    else:
        directory.mkdir(parents=True, exist_ok=True)
        broker_dir = Path(
            tempfile.mkdtemp(prefix=f"{child_id}-broker-", dir=str(directory))
        )
        payload, error, pid, killed = _run_in_subprocess(
            _child_spec(
                child_id=child_id,
                objective=objective,
                workspace=workspace,
                model=model,
                profile=profile,
                ledger_dir=directory,
                permission=permission,
                max_steps=max_steps,
                parent_task_id=parent_task_id,
                broker_dir=str(broker_dir),
            ),
            ledger_dir=directory,
            timeout=timeout,
            child_id=child_id,
            approval=approval,
            broker_dir=broker_dir,
        )

    elapsed = round(time.time() - started, 3)
    if payload is None:
        return ChildOutcome(
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
        )
    return ChildOutcome(
        child_id=child_id,
        objective=objective,
        profile=profile,
        accepted=bool(payload.get("accepted")),
        phase=str(payload.get("phase") or "failed"),
        answer=str(payload.get("answer") or ""),
        ledger_path=str(payload.get("ledger_path") or directory / f"{child_id}.jsonl"),
        debt=tuple(payload.get("debt") or ()),
        tool_calls=int(payload.get("tool_calls") or 0),
        elapsed_seconds=elapsed,
        error=error,
        pid=pid,
        killed=killed,
    )


def _run_in_process(
    *,
    objective: str,
    workspace: str | Path,
    model: ModelConfig,
    profile: str,
    child_id: str,
    parent_task_id: str,
    directory: Path,
    permission: PermissionMode,
    max_steps: int,
    timeout: float | None,
    client: Any,
    gateway_context: Any,
    approval: Any = None,
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
    max_steps: int = 20,
    timeout: float | None = None,
    client: Any = None,
    approval: Any = None,
    parent_permission: PermissionMode | None = None,
    max_workers: int = 4,
) -> tuple[ChildOutcome, ...]:
    """Run delegated tasks in parallel, preserving input order."""

    objectives = tuple(objective for objective in objectives if objective.strip())
    if not objectives:
        return ()
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
                ledger_dir=ledger_dir,
                permission=permission,
                max_steps=max_steps,
                timeout=timeout,
                client=client,
                approval=approval,
                parent_permission=parent_permission,
                index=index,
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
    }


__all__ = [
    "CHILD_PROFILES",
    "MAX_CHILD_DEPTH",
    "ChildOutcome",
    "child_evidence",
    "run_child_task",
    "run_child_tasks",
]
