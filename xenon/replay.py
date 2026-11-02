"""Read-only replay of a finished task.

Two questions a ledger should be able to answer without a model:

- **what happened, in order?** (``timeline``)
- **what did it cost and where did it stop?** (``metrics``)

Both are pure folds over events, so the same ledger always produces the same
report — that is the point of an append-only record.  Nothing here writes,
and nothing here reads the workspace: a replay must work after the worktree
has been cleaned up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "blocking_reason",
    "default_ledger_dir",
    "resolve_ledger",
    "timeline",
    "metrics",
    "render_timeline",
    "render_metrics",
]

# Events worth showing in a timeline; the rest are bookkeeping noise.
_TIMELINE_KINDS = frozenset(
    {
        "task_started",
        "intake_recorded",
        "plan_proposed",
        "plan_accepted",
        "plan_approved",
        "plan_reviewed",
        "step_started",
        "step_closed",
        "step_verified",
        "tool_receipt",
        "claim_proposed",
        "claim_resolved",
        "debt_opened",
        "debt_closed",
        "audit_reported",
        "policy_decision",
        "control_decision",
        "context_compacted",
        "model_usage",
        "user_input",
        "user_question",
        "user_approval",
        "delivery_attempted",
        "delivered",
        "finalized",
        "state_transition",
    }
)


def default_ledger_dir() -> Path:
    return Path.home() / ".xenon" / "ledger"


def resolve_ledger(target: str | Path, *, root: Path | None = None) -> Path:
    """Find a ledger from a task id or a path.

    A task id is the common case (``xenon replay <task-id>``); a path lets a
    CI job point at a downloaded artifact.
    """

    candidate = Path(str(target)).expanduser()
    if candidate.is_file():
        return candidate
    directory = (root or default_ledger_dir()).expanduser()
    by_id = directory / f"{target}.jsonl"
    if by_id.is_file():
        return by_id
    raise FileNotFoundError(
        f"no ledger for {target!r}: looked for {candidate} and {by_id}"
    )


def _payload(event: Any) -> dict[str, Any]:
    payload = getattr(event, "payload", {})
    return dict(payload) if isinstance(payload, dict) else {}


def _line(event: Any) -> str:
    kind = getattr(getattr(event, "kind", None), "value", "")
    payload = _payload(event)
    if kind == "tool_receipt":
        receipt = payload.get("receipt") or {}
        tool = receipt.get("tool") or "?"
        status = receipt.get("status") or "?"
        return f"{tool} -> {status}"
    if kind in {"plan_proposed", "plan_accepted"}:
        plan = payload.get("plan") or {}
        steps = plan.get("steps") or []
        return f"{len(steps)} step(s)"
    if kind == "step_closed":
        return str(payload.get("step_id") or "")
    if kind == "debt_opened":
        return f"{payload.get('obligation_id') or '?'}: {payload.get('reason') or ''}"
    if kind == "debt_closed":
        return str(payload.get("obligation_id") or "")
    if kind == "model_usage":
        return (
            f"{payload.get('total_tokens', 0)} tokens "
            f"(cumulative {payload.get('cumulative_total', 0)})"
        )
    if kind == "context_compacted":
        return f"folded {payload.get('count', 0)} event(s)"
    if kind == "user_input":
        return f"{payload.get('kind') or 'note'}: {str(payload.get('text') or '')[:60]}"
    if kind == "policy_decision":
        return f"{payload.get('kind') or '?'}: {payload.get('reason') or ''}"
    if kind == "control_decision":
        return f"{payload.get('kind') or '?'}: {payload.get('reason') or ''}"
    if kind in {"audit_reported", "delivered", "finalized"}:
        return str(payload.get("evidence_status") or payload.get("commit") or "")
    if kind == "state_transition":
        return f"{payload.get('from')} -> {payload.get('to')}"
    text = payload.get("text") or payload.get("objective") or ""
    return str(text)[:80]


def timeline(events: Iterable[Any], *, include_all: bool = False) -> list[dict[str, Any]]:
    """Ordered, human-readable account of the run."""

    rows: list[dict[str, Any]] = []
    for event in events:
        kind = getattr(getattr(event, "kind", None), "value", "")
        if not include_all and kind not in _TIMELINE_KINDS:
            continue
        rows.append(
            {
                "seq": int(getattr(event, "seq", 0) or 0),
                "kind": kind,
                "source": getattr(getattr(event, "source", None), "value", ""),
                "step": getattr(event, "step_id", None),
                "line": _line(event),
            }
        )
    return rows


def metrics(events: Sequence[Any]) -> dict[str, Any]:
    """Deterministic totals: cost, work done, and where it stopped."""

    tokens = 0
    estimated_calls = 0
    model_calls = 0
    tool_calls = 0
    steps = 0
    compactions = 0
    foldings = 0
    steered = 0
    approvals = 0
    repairs = 0
    replans = 0
    audit_rounds = 0
    debt_open: list[str] = []
    debt_closed: list[str] = []
    phase = ""
    reason = ""
    finalize_sha = ""
    evidence = ""
    accepted = False
    first_ts = ""
    last_ts = ""

    for event in events:
        kind = getattr(getattr(event, "kind", None), "value", "")
        payload = _payload(event)
        ts = str(getattr(event, "ts", "") or "")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        if kind == "model_usage":
            model_calls += 1
            tokens += int(payload.get("total_tokens") or 0)
            if payload.get("estimated"):
                estimated_calls += 1
        elif kind == "tool_receipt":
            tool_calls += 1
        elif kind == "step_started":
            steps += 1
        elif kind == "context_compacted":
            compactions += 1
            foldings += int(payload.get("count") or 0)
        elif kind == "user_input":
            steered += 1
        elif kind == "user_approval":
            approvals += 1
        elif kind == "binding_failed":
            replans += 1
        elif kind == "audit_reported":
            audit_rounds += 1
        elif kind == "debt_opened":
            debt_open.append(str(payload.get("obligation_id") or ""))
        elif kind == "debt_closed":
            item = str(payload.get("obligation_id") or "")
            debt_closed.append(item)
            debt_open = [entry for entry in debt_open if entry != item]
        elif kind == "policy_decision":
            if str(payload.get("reason") or "").startswith("repeated identical"):
                repairs += 1
        elif kind == "state_transition":
            phase = str(payload.get("to") or phase)
            reason = str(payload.get("reason") or reason)
        elif kind == "finalized":
            finalize_sha = str(payload.get("commit") or finalize_sha)
            evidence = str(payload.get("evidence_status") or evidence)
            accepted = True
        elif kind == "delivered":
            evidence = str(payload.get("evidence_status") or evidence)
            finalize = payload.get("finalize") or {}
            if isinstance(finalize, dict):
                finalize_sha = str(finalize.get("commit") or finalize_sha)

    return {
        "events": len(events),
        "model_calls": model_calls,
        "tokens": tokens,
        "estimated_calls": estimated_calls,
        "tool_calls": tool_calls,
        "steps": steps,
        "compactions": compactions,
        "folded_events": foldings,
        "steering_inputs": steered,
        "approvals": approvals,
        "replans": replans,
        "repair_vetoes": repairs,
        "audit_rounds": audit_rounds,
        "open_debt": debt_open,
        "closed_debt": debt_closed,
        "final_phase": phase,
        "final_reason": reason,
        "finalize_sha": finalize_sha,
        "evidence_status": evidence,
        "accepted": accepted,
        "started_at": first_ts,
        "ended_at": last_ts,
    }


# Events that explain *why* a run stopped.
_BLOCKING_KINDS = (
    "policy_decision",
    "user_approval",
    "control_decision",
    "binding_failed",
)


def blocking_reason(ledger_path: Any, *, task_id: str = "") -> str:
    """Last human-readable reason the run stopped, or an empty string.

    A blocked result carries only the phase, so without this a caller sees
    "blocked" and nothing else.  The ledger knows why; read it.
    """

    try:
        from xenon.ledger.store import Ledger

        events = list(Ledger(ledger_path, task_id or Path(str(ledger_path)).stem).read())
    except Exception:  # noqa: BLE001 — reporting must never fail the caller
        return ""
    for event in reversed(events):
        kind = getattr(getattr(event, "kind", None), "value", "")
        if kind not in _BLOCKING_KINDS:
            continue
        payload = dict(getattr(event, "payload", {}) or {})
        reason = str(payload.get("reason") or "")
        if kind == "user_approval":
            if payload.get("approved") is False and reason:
                return f"审批未通过：{reason}"
            continue
        if reason:
            scope = payload.get("scope") or ()
            if scope:
                return f"{reason}（范围：{', '.join(str(item) for item in scope)}）"
            return reason
    return ""


def render_timeline(rows: Sequence[dict[str, Any]]) -> str:
    return "\n".join(
        f"{row['seq']:>5}  {row['kind']:<20} {row['line']}" for row in rows
    )


def render_metrics(data: dict[str, Any]) -> str:
    lines = [
        f"events        {data['events']}",
        f"model calls   {data['model_calls']}  (estimated {data['estimated_calls']})",
        f"tokens        {data['tokens']}",
        f"tool calls    {data['tool_calls']}",
        f"steps         {data['steps']}",
        f"compactions   {data['compactions']}  (folded {data['folded_events']} events)",
        f"steering      {data['steering_inputs']}  approvals {data['approvals']}",
        f"replans       {data['replans']}  repair vetoes {data['repair_vetoes']}",
        f"audit rounds  {data['audit_rounds']}",
        f"final phase   {data['final_phase']}  ({data['final_reason']})",
        f"accepted      {data['accepted']}  evidence {data['evidence_status'] or '-'}",
        f"finalize      {data['finalize_sha'] or '-'}",
        f"open debt     {', '.join(data['open_debt']) or '-'}",
    ]
    return "\n".join(lines)
