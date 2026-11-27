"""Read-only queries behind the local dashboard.

The dashboard is a *view* of the same session the terminal drives.  It must not
become a second source of truth: every answer here is derived from facts the
session already produced — the last run's result, its ledger file, the
workspace, and the stores the REPL owns.  Nothing in this module writes, runs
tools, or changes kernel state.

Each method returns a plain dict; the host puts it on the wire with
``<query>.result``.  A query that cannot answer returns an ``error`` key rather
than raising: a broken panel must never take the session down with it.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = ["DashboardQueries"]

#: Hard ceilings: a panel is a view, not a bulk export.
MAX_DIFF_BYTES = 200_000
MAX_DIFF_FILES = 100
MAX_LEDGER_EVENTS = 500
MAX_MEMORIES = 200
MAX_SESSION_LOGS = 100
GIT_TIMEOUT = 8.0


def _loop_label(repl: Any) -> str:
    """The one loop, described by the phases it runs.

    Reads the same formatter as ``/mode`` and the status bar so the panel
    cannot disagree with the terminal.  An unreadable registry returns the
    bare label rather than inventing phases.
    """

    from xenon.modes import describe_loop

    profile = None
    registry = getattr(repl, "registry", None)
    get_mode = getattr(registry, "get_current_mode", None)
    if callable(get_mode):
        try:
            profile = getattr(get_mode(), "loop", None)
        except Exception:  # noqa: BLE001 — a label must not fail a panel
            profile = None
    return describe_loop(profile)


def _jsonable(value: Any, *, depth: int = 0) -> Any:
    """Coerce kernel objects into something ``json.dumps`` accepts.

    Event payloads are mostly plain dicts, but verdicts and claims are frozen
    dataclasses.  A panel that shows a dataclass as ``<object at 0x…>`` is
    useless, and one that crashes the host is worse.
    """

    if depth > 6:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    # Enums first: ``str`` subclasses (EventKind, EvidenceStatus) are plain
    # strings to json, but Path-like and dataclass values are not.
    if not isinstance(value, str) and hasattr(value, "value"):  # noqa: SIM102
        inner = value.value
        if isinstance(inner, (str, int, float, bool)):
            return inner
    if is_dataclass(value) and not isinstance(value, type):
        try:
            return _jsonable(asdict(value), depth=depth + 1)
        except Exception:  # noqa: BLE001 — slots/dataclass edge cases
            return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item, depth=depth + 1) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value") and isinstance(value.value, (str, int)):
        return value.value
    return str(value)


#: Events that only say "the loop moved on", or restate a fact the row above
#: already carries.  They are the ledger's spine — essential to replay — and
#: noise to a reader asking what a turn actually did (#441).  A view hides
#: them and reports the count; the ledger itself is untouched.
_HIDDEN_KINDS = frozenset(
    {
        "state_transition",
        "model_usage",
        "thought",
        "session_context",
        "workspace_probed",
        "workspace_layout",
        "intake_recorded",
        "step_verified",
        "step_closed",
    }
)


def _fold_bookkeeping(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Hide bookkeeping events from a reading view; return what was hidden.

    Hiding is *reported*, never silent: the caller prints "折叠 N 条（按 kind
    计数）" so a reader can see that the record has more in it.  A verdict that
    is not ``satisfied`` is never hidden — that is the reason to open the table.
    """

    rows: list[dict[str, Any]] = []
    hidden: dict[str, int] = {}
    for event in events:
        kind = str(event.get("kind"))
        payload = event.get("payload") or {}
        drop = kind in _HIDDEN_KINDS or (
            kind == "step_verdict" and str(payload.get("status")) == "satisfied"
        )
        if drop:
            hidden[kind] = hidden.get(kind, 0) + 1
            continue
        rows.append(event)
    return rows, hidden


class DashboardQueries:
    """Answers for the dashboard's read-only query frames."""

    def __init__(self, host: Any) -> None:
        self.host = host

    # ── plumbing ───────────────────────────────────────────────────
    @property
    def cwd(self) -> Path:
        return Path(getattr(self.host, "cwd", Path.cwd()))

    @property
    def repl(self) -> Any:
        return getattr(self.host, "repl", None)

    @property
    def last_run(self) -> Any:
        return getattr(self.host, "last_run", None)

    @property
    def ledger_path(self) -> Path | None:
        path = getattr(self.host, "last_ledger", None)
        if path is None:
            run = self.last_run
            path = getattr(run, "ledger_path", None)
        return Path(path) if path is not None else None

    def _result(self) -> Any:
        run = self.last_run
        return getattr(run, "result", None)

    def _recorded_result(self) -> Mapping[str, Any]:
        """The kernel's own result file, when it is not this process's run.

        A resumed session owns a ledger and a ``.result.json`` but no live
        ``HarnessRun``: ``/resume`` restores the pointer, not the run object.
        The panel then has to read the same result the kernel wrote, or it
        says "未报告" for evidence the terminal shows through ``/evidence``.
        """

        path = self.ledger_path
        run = self.last_run
        if path is None:
            return {}
        run_path = getattr(run, "ledger_path", None)
        if run is not None and run_path and Path(str(run_path)) == path:
            return {}
        try:
            from xenon.repl.kernel_evidence import load_kernel_evidence

            evidence = load_kernel_evidence(path)
        except Exception:  # noqa: BLE001 — a panel fact, never a session failure
            return {}
        if evidence is None or not isinstance(evidence.result, Mapping):
            return {}
        return dict(evidence.result)

    def _evidence_view(self) -> tuple[str, Any]:
        """``(status, accepted)`` from the run, else from the ledger result."""

        result = self._result()
        status = getattr(result, "evidence_status", None)
        accepted = getattr(result, "accepted", None)
        if status is None:
            recorded = self._recorded_result()
            if recorded:
                status = recorded.get("evidence_status")
                accepted = recorded.get("accepted")
        return str(getattr(status, "value", status) or "未报告"), accepted

    def _open_ledger(self) -> Any:
        path = self.ledger_path
        if path is None or not path.exists():
            return None
        from xenon.ledger.store import Ledger

        task_id = str(getattr(self.last_run, "task_id", "") or path.stem)
        try:
            return Ledger.open(path, task_id=task_id)
        except Exception:  # noqa: BLE001 — an unreadable ledger is a panel state
            return None

    def _git(self, *args: str) -> str | None:
        """Run one read-only git command inside the workspace.

        ``stdin`` is closed on purpose.  The host's stdin is the NDJSON pipe
        the front-end writes frames to — and a child that inherits it while the
        host loop is blocked reading that same pipe can deadlock: every git
        call then sat until ``GIT_TIMEOUT`` and the panel timed out first.
        A read-only query has no business reading the user's input stream.
        """

        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.cwd), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT,
                check=False,
                stdin=subprocess.DEVNULL,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    # ── shared facts ───────────────────────────────────────────────
    def _working_memory(self) -> dict[str, Any]:
        context = getattr(self.repl, "ctx_mgr", None)
        getter = getattr(context, "get_working_memory", None)
        if not callable(getter):
            return {}
        try:
            return dict(getter() or {})
        except Exception:  # noqa: BLE001 — memory is optional
            return {}

    def _session_paths(self) -> tuple[list[str], list[str]]:
        memory = self._working_memory()
        return (
            [str(item) for item in list(memory.get("session_created_files") or [])],
            [str(item) for item in list(memory.get("session_modified_files") or [])],
        )

    def _delivery(self) -> dict[str, Any] | None:
        """The delivery record behind the last turn, from the ledger.

        ``finalize_sha`` on the result is the commit; when the run never
        finalized this stays ``None`` and the panel says so rather than
        guessing from a dirty worktree.
        """

        payload: Mapping[str, Any] | None = None
        ledger = self._open_ledger()
        if ledger is not None:
            try:
                for event in ledger.read():
                    if str(getattr(event.kind, "value", event.kind)) == "delivered":
                        raw = event.payload or {}
                        payload = raw if isinstance(raw, Mapping) else None
            except Exception:  # noqa: BLE001 — delivery is a panel fact
                payload = None
        finalize: Mapping[str, Any] = {}
        if isinstance(payload, Mapping):
            raw_finalize = payload.get("finalize")
            if isinstance(raw_finalize, Mapping):
                finalize = raw_finalize
        result = self._result()
        commit = finalize.get("commit") or getattr(result, "finalize_sha", None)
        verified = [str(item) for item in list(finalize.get("verified_paths") or [])]
        unverified = [str(item) for item in list(finalize.get("unverified_paths") or [])]
        attributed = [str(item) for item in list(finalize.get("attributed_paths") or [])]
        if not commit and not verified and not unverified and not attributed:
            return None
        return {
            "commit": str(commit) if commit else None,
            "verified_paths": verified,
            "unverified_paths": unverified,
            "attributed_paths": attributed,
        }

    @staticmethod
    def _normalized(path: str) -> str:
        text = str(path).replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        return text

    @classmethod
    def _covers(cls, path: str, candidates: list[str]) -> bool:
        target = cls._normalized(path)
        for candidate in candidates:
            other = cls._normalized(candidate)
            if other == target or other.endswith("/" + target):
                return True
        return False

    # ── the panels ─────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        """Everything a first paint needs, in one round trip."""

        host = self.host
        result = self._result()
        facts: dict[str, Any] = {}
        status_facts = getattr(host, "_status_facts", None)
        if callable(status_facts):
            try:
                facts = dict(status_facts() or {})
            except Exception:  # noqa: BLE001 — a status panel may omit facts
                facts = {}
        models: list[str] = []
        current = getattr(host, "_current_models", None)
        if callable(current):
            try:
                models = [str(m) for m in current()]
            except Exception:  # noqa: BLE001
                models = []
        ledger = self.ledger_path
        debt = []
        for item in list(getattr(result, "debt", None) or [])[:20]:
            obligation = getattr(item, "obligation", None)
            debt.append(
                {
                    "kind": str(
                        getattr(getattr(obligation, "kind", ""), "value", "")
                        or getattr(obligation, "kind", "")
                    ),
                    "reason": str(
                        getattr(item, "reason", "") or getattr(obligation, "id", "")
                    ),
                    "obligation_id": str(getattr(obligation, "id", "") or ""),
                    "target": str(getattr(obligation, "target", "") or ""),
                    "next_action": str(getattr(item, "next_action", "") or ""),
                    "attempts": int(getattr(item, "attempts", 0) or 0),
                    "acceptable": bool(getattr(item, "acceptable", True)),
                    "user_accepted": bool(getattr(item, "user_accepted", False)),
                }
            )
        evidence_status, evidence_accepted = self._evidence_view()
        return {
            "cwd": str(self.cwd),
            "models": models,
            # The panel shows the loop and its phases, not the internal preset
            # name: a "mode" the user cannot switch is not a fact worth showing.
            "mode": _loop_label(self.repl),
            "turn": {
                "id": str(getattr(host, "turn", "") or ""),
                "objective": str(getattr(host, "turn_objective", "") or ""),
                "running": bool(
                    getattr(host, "turn_thread", None) is not None
                    and getattr(host.turn_thread, "is_alive", lambda: False)()
                ),
                "elapsed_s": (
                    round(
                        max(
                            0.0,
                            time.time() - float(getattr(host, "turn_started_at", 0.0)),
                        ),
                        1,
                    )
                    if getattr(host, "turn_started_at", 0.0)
                    else 0.0
                ),
            },
            "status": _jsonable(facts),
            "ledger": str(ledger) if ledger else None,
            "evidence": {
                "status": evidence_status,
                "accepted": evidence_accepted,
            },
            "debt": debt,
            "delivery": self._delivery(),
            "approvals": [
                str(getattr(item, "message_id", ""))
                for item in list(getattr(host, "_pending", {}).values())
                if not getattr(item, "settled", None) or not item.settled.is_set()
            ],
            "prompts": [
                str(message_id)
                for message_id, event in list(
                    getattr(host, "_prompt_waiters", {}).items()
                )
                if not event.is_set()
            ],
        }

    def workspace(self) -> dict[str, Any]:
        """Branch, head and the files this session (or git) says changed."""

        is_git = self._git("rev-parse", "--is-inside-work-tree") is not None
        branch = (self._git("rev-parse", "--abbrev-ref", "HEAD") or "").strip() or None
        head = (self._git("rev-parse", "--short", "HEAD") or "").strip() or None
        dirty: list[dict[str, str]] = []
        porcelain = self._git("status", "--porcelain=v1", "-z") or ""
        for entry in porcelain.split("\0"):
            if not entry.strip():
                continue
            # Porcelain v1 is ``XY <path>``; the leading status column is a
            # space for an unstaged change, so splitting on the first space
            # misread " M tracked.txt" as a path of "M tracked.txt".
            dirty.append({"status": entry[:2].strip(), "path": entry[2:].strip()})
        created, modified = self._session_paths()
        baseline = [
            item
            for item in dirty
            if not self._covers(item["path"], created)
            and not self._covers(item["path"], modified)
        ]
        return {
            "cwd": str(self.cwd),
            "is_git": is_git,
            "branch": branch,
            "head": head,
            "dirty": dirty[:200],
            "dirty_count": len(dirty),
            "baseline": baseline[:200],
            "baseline_count": len(baseline),
            "session_created": _jsonable(created),
            "session_modified": _jsonable(modified),
            "delivery": self._delivery(),
            "ledger": str(self.ledger_path) if self.ledger_path else None,
        }

    def diff(self) -> dict[str, Any]:
        """The workspace diff behind the answer, bounded and read-only."""

        if self._git("rev-parse", "--is-inside-work-tree") is None:
            return {
                "files": [],
                "diff": "",
                "baseline": [],
                "baseline_count": 0,
                "delivery": None,
                "error": "工作区不是 git 仓库",
            }
        porcelain = self._git("status", "--porcelain=v1", "-z") or ""
        files: list[dict[str, str]] = []
        for entry in porcelain.split("\0"):
            if not entry.strip():
                continue
            files.append({"status": entry[:2].strip(), "path": entry[2:].strip()})
            if len(files) >= MAX_DIFF_FILES:
                break
        # ``--no-ext-diff`` keeps an external diff driver out of the panel, and
        # the path list keeps a giant worktree from producing a giant frame.
        paths = [item["path"] for item in files]
        patch = ""
        if paths:
            patch = self._git("diff", "--no-color", "--no-ext-diff", "--", *paths) or ""
            # Untracked files have no diff of their own; show their content so a
            # "new file" row is not an empty panel.
            untracked = [item["path"] for item in files if item["status"].startswith("??")]
            for path in untracked[:10]:
                target = (self.cwd / path).resolve()
                try:
                    target.relative_to(self.cwd.resolve())
                    text = target.read_text(encoding="utf-8", errors="replace")
                except (OSError, ValueError):
                    continue
                patch += f"\n--- /dev/null\n+++ b/{path}\n{text}"
                if len(patch) > MAX_DIFF_BYTES:
                    break
        created, modified = self._session_paths()
        delivery = self._delivery() or {}
        verified = list(delivery.get("verified_paths") or [])
        attributed = list(delivery.get("attributed_paths") or [])
        baseline: list[dict[str, str]] = []
        for item in files:
            if self._covers(item["path"], created):
                item["origin"] = "created"
            elif self._covers(item["path"], modified):
                item["origin"] = "modified"
            else:
                item["origin"] = "baseline"
                baseline.append(item)
            if self._covers(item["path"], verified):
                item["verification"] = "verified"
            elif item["origin"] != "baseline" or self._covers(item["path"], attributed):
                item["verification"] = "attributed"
            else:
                item["verification"] = "unverified"
        # A finalized turn commits its files, so the worktree is clean and the
        # loop above finds nothing.  The panel answers "what did this turn
        # change", so fall back to the delivery commit for the paths the
        # delivery record names -- never a blanket ``git show`` of whatever
        # else that commit happened to contain.
        commit = str(delivery.get("commit") or "")
        committed = [
            path
            for path in (verified + attributed)
            if not any(self._covers(item["path"], [path]) for item in files)
        ]
        if commit and committed:
            names = (
                self._git(
                    "show",
                    "--name-status",
                    "--format=",
                    "--no-color",
                    "--no-ext-diff",
                    "--no-renames",
                    commit,
                    "--",
                    *committed,
                )
                or ""
            )
            for line in names.splitlines():
                parts = line.split("\t")
                if len(parts) < 2 or not parts[-1].strip():
                    continue
                path = parts[-1].strip()
                status = (parts[0].strip()[:1] or "M").upper()
                files.append(
                    {
                        "status": status,
                        "path": path,
                        "origin": "created" if status == "A" else "modified",
                        "verification": (
                            "verified"
                            if self._covers(path, verified)
                            else "attributed"
                        ),
                    }
                )
            committed_patch = (
                self._git(
                    "show",
                    "--no-color",
                    "--no-ext-diff",
                    "--format=",
                    "--no-renames",
                    commit,
                    "--",
                    *committed,
                )
                or ""
            )
            if committed_patch:
                patch = f"{patch}\n{committed_patch}" if patch else committed_patch
        return {
            "files": files,
            "diff": patch[:MAX_DIFF_BYTES],
            "truncated": len(patch) > MAX_DIFF_BYTES,
            "baseline": baseline,
            "baseline_count": len(baseline),
            "delivery": delivery or None,
            "ledger": str(self.ledger_path) if self.ledger_path else None,
        }

    def ledger_events(
        self,
        *,
        kinds: list[str] | None = None,
        limit: int = 200,
        fold: bool = False,
    ) -> dict[str, Any]:
        ledger = self._open_ledger()
        if ledger is None:
            return {"events": [], "chain": None, "error": "没有可读的账本"}
        wanted = {str(k) for k in kinds or [] if str(k)}
        events = []
        for event in ledger.read():
            kind = str(getattr(event.kind, "value", event.kind))
            if wanted and kind not in wanted:
                continue
            events.append(
                {
                    "seq": int(event.seq),
                    "kind": kind,
                    "ts": str(getattr(event, "ts", "") or ""),
                    "source": str(getattr(event, "source", "") or ""),
                    "hash": str(getattr(event, "hash", "") or ""),
                    "payload": _jsonable(event.payload),
                }
            )
        limit = max(1, min(int(limit or 200), MAX_LEDGER_EVENTS))
        total = len(events)
        window = events[-limit:]
        hidden: dict[str, int] = {}
        shown: list[dict[str, Any]] = window
        if fold:
            shown, hidden = _fold_bookkeeping(window)
        return {
            "events": shown,
            "total": total,
            "shown": len(shown),
            "folded": sum(hidden.values()),
            "hidden_by_kind": hidden,
            "chain": self._chain_report(ledger),
            "ledger": str(self.ledger_path),
        }

    def _chain_report(self, ledger: Any) -> dict[str, Any]:
        try:
            report = ledger.verify()
        except Exception as exc:  # noqa: BLE001
            return {"valid": False, "reason": f"{type(exc).__name__}: {exc}"}
        return {
            "valid": bool(getattr(report, "valid", False)),
            "reason": str(getattr(report, "reason", "") or ""),
            "events": int(getattr(report, "events", 0) or 0),
            "last_seq": int(getattr(report, "last_seq", 0) or 0),
            "last_hash": str(getattr(report, "last_hash", "") or ""),
            "truncated_tail": bool(getattr(report, "truncated_tail", False)),
        }

    def evidence_chain(self) -> dict[str, Any]:
        """fact → verdict → claim → delivery, taken from the ledger alone."""

        ledger = self._open_ledger()
        if ledger is None:
            status, _accepted = self._evidence_view()
            return {
                "facts": [],
                "verdicts": [],
                "claims": [],
                "delivered": None,
                "status": status,
                "projection": {},
                "chain": None,
                "error": "没有可读的账本",
            }
        from xenon.ledger.projections import project

        facts, verdicts, claims = [], [], []
        delivered = None
        for event in ledger.read():
            kind = str(getattr(event.kind, "value", event.kind))
            payload = _jsonable(event.payload)
            # The kernel's vocabulary, not a guess: a receipt is the fact, a
            # step verdict is the check, a claim is what the answer asserts.
            if kind in {"tool_receipt", "tool_interrupted"}:
                facts.append({"seq": event.seq, "payload": payload})
            elif kind in {"step_verdict", "step_verified"}:
                verdicts.append({"seq": event.seq, "payload": payload})
            elif kind in {"claim_proposed", "claim_resolved"}:
                claims.append({"seq": event.seq, "payload": payload})
            elif kind == "delivered":
                delivered = {"seq": event.seq, "payload": payload}
        try:
            evidence = project("evidence", ledger.read())
        except Exception:  # noqa: BLE001
            evidence = {}
        status, _accepted = self._evidence_view()
        return {
            "facts": facts,
            "verdicts": verdicts,
            "claims": claims,
            "delivered": delivered,
            "status": status,
            "projection": _jsonable(evidence),
            "chain": self._chain_report(ledger),
            "ledger": str(self.ledger_path),
        }

    def _plan_decision(self, ledger_path: Path) -> tuple[Mapping[str, Any] | None, str]:
        """The user's plan decision, from the same source ``/plan`` reads.

        The kernel writes ``plan_accepted`` whenever it *proposes* a plan — it
        means "the runtime accepted its own plan and is parked at PLANNED", not
        "the operator approved it".  Reading that event as consent made every
        plan-only turn look approved before ``/plan approve`` was ever typed.
        The approval fact lives in the REPL session events, so this delegates
        to the canonical resolver instead of re-deriving consent from the
        ledger.  Returns ``(decision, phase)``; both may be empty.
        """

        repl = self.repl
        if repl is None:
            return None, ""
        try:
            from xenon.repl.kernel_evidence import (
                load_kernel_evidence,
                resolve_plan_approval,
            )

            evidence = load_kernel_evidence(ledger_path)
            if evidence is None:
                return None, ""
            phase = str(evidence.task_state.get("phase") or "")
            decision = resolve_plan_approval(repl, evidence)
            return decision, phase
        except Exception:  # noqa: BLE001 — a panel state, not a session failure
            return None, ""

    def plan(self) -> dict[str, Any]:
        ledger = self._open_ledger()
        if ledger is None:
            return {
                "plan": None,
                "steps": {},
                "accepted": False,
                "proposed": False,
                "rejected": False,
                "current": None,
                "ledger": str(self.ledger_path) if self.ledger_path else None,
                "error": "没有可读的账本",
            }
        try:
            projection = ledger.project("plan")
        except Exception as exc:  # noqa: BLE001
            return {
                "plan": None,
                "steps": {},
                "accepted": False,
                "proposed": False,
                "rejected": False,
                "current": None,
                "ledger": str(self.ledger_path) if self.ledger_path else None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        has_plan = False
        delivered = False
        try:
            for event in ledger.read():
                kind = str(getattr(event.kind, "value", event.kind))
                if kind == "plan_proposed":
                    has_plan = True
                elif kind == "delivered":
                    delivered = True
        except Exception:  # noqa: BLE001 — status flags are optional
            pass

        accepted = False
        proposed = False
        rejected = False
        decision, phase = (
            self._plan_decision(self.ledger_path)
            if self.ledger_path is not None
            else (None, "")
        )
        status = str((decision or {}).get("status") or "")
        finished_phases = {
            "finalized",
            "delivered",
            "failed",
            "blocked",
            "aborted",
        }
        if status == "approved":
            accepted = True
        elif status == "rejected":
            rejected = True
        elif has_plan:
            # No user decision: a delivered/finished run executed its plan on
            # its own; anything else with a plan is still waiting for consent.
            if delivered or phase in finished_phases:
                accepted = True
            else:
                proposed = True

        current = None
        steps = projection.get("steps") if isinstance(projection, Mapping) else None
        if isinstance(steps, Mapping):
            for step_id, item in steps.items():
                if isinstance(item, Mapping) and item.get("status") == "running":
                    current = str(step_id)
                    break
        return {
            "plan": _jsonable(projection),
            "accepted": accepted,
            "proposed": proposed,
            "rejected": rejected,
            "current": current,
            "ledger": str(self.ledger_path),
        }

    def memory(self) -> dict[str, Any]:
        service = None
        getter = getattr(self.repl, "_get_memory_service", None)
        if callable(getter):
            try:
                service = getter()
            except Exception:  # noqa: BLE001 — memory is optional
                service = None
        items: list[dict[str, Any]] = []
        if service is not None:
            try:
                for memory in service.list_all()[:MAX_MEMORIES]:
                    items.append(_jsonable(memory))
            except Exception:  # noqa: BLE001
                items = []
        return {"items": items, "count": len(items)}

    def mcp(self) -> dict[str, Any]:
        servers: list[dict[str, Any]] = []
        tools: list[str] = []
        registry = getattr(self.repl, "_mcp_registry", None)
        if registry is not None:
            for attr in ("list_servers", "servers"):
                source = getattr(registry, attr, None)
                if callable(source):
                    try:
                        source = source()
                    except Exception:  # noqa: BLE001
                        source = None
                if source:
                    try:
                        for server in source:
                            servers.append(_jsonable(server))
                    except TypeError:
                        for name, server in getattr(source, "items", lambda: [])():
                            servers.append({"name": str(name), **_jsonable(server)})
                    break
            tool_map = getattr(registry, "tool_map", None)
            if isinstance(tool_map, Mapping):
                tools = sorted(str(name) for name in tool_map)
        return {"servers": servers, "tools": tools}

    def report(self) -> dict[str, Any]:
        payload = None
        builder = getattr(self.host, "_report_payload", None)
        if callable(builder):
            try:
                payload = builder()
            except Exception:  # noqa: BLE001
                payload = None
        return payload or {"models": [], "totals": None, "error": "本会话还没有模型调用"}

    def sessions(self) -> dict[str, Any]:
        from xenon.repl.session import list_sessions

        try:
            rows = list_sessions()
        except Exception as exc:  # noqa: BLE001
            return {"sessions": [], "error": str(exc)[:500]}
        return {
            "sessions": [
                {
                    "name": str(row.get("name") or ""),
                    "saved_at": str(row.get("saved_at") or ""),
                    "messages": int(row.get("messages") or 0),
                }
                for row in rows
            ]
        }

    def sessions_tree(self) -> dict[str, Any]:
        """The fork/rewind tree, read from the session event logs.

        Saved sessions are snapshots; the tree lives in the append-only event
        logs (`turn/user`, `fork`).  This is read-only: the panel renders what
        the logs already recorded, including a fork's ``parent``.
        """

        from xenon.session.events import session_events_dir

        directory = Path(session_events_dir())
        if not directory.exists():
            return {"nodes": [], "roots": [], "error": "没有会话事件日志"}
        nodes: list[dict[str, Any]] = []
        try:
            files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        except OSError as exc:
            return {"nodes": [], "roots": [], "error": str(exc)[:500]}
        for path in files[-MAX_SESSION_LOGS:]:
            try:
                events = []
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(record, dict) and record.get("type"):
                            events.append(record)
            except OSError:
                continue
            if not events:
                continue
            parent = None
            turns = 0
            for event in events:
                if event.get("type") == "turn/user":
                    turns += 1
                elif event.get("type") == "fork":
                    parent = str(event.get("parent") or "") or None
            tries = [event.get("at") for event in events if isinstance(event.get("at"), (int, float))]
            nodes.append(
                {
                    "id": path.stem,
                    "parent": parent,
                    "turns": turns,
                    "events": len(events),
                    "first_at": min(tries) if tries else None,
                    "last_at": max(tries) if tries else None,
                }
            )
        known = {node["id"] for node in nodes}
        roots = [node["id"] for node in nodes if not node["parent"] or node["parent"] not in known]
        return {"nodes": nodes, "roots": roots}


def dumps(payload: Mapping[str, Any]) -> str:
    """Debug helper used by tests: stable, UTF-8 JSON."""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
