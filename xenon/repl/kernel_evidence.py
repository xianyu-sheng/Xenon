"""Read-only views over kernel ledgers for /evidence and /resume (#236).

Kernel ledgers are the authoritative facts for kernel turns.  This module only
reads them: it never replays a task, restores an approval, or mutates state.
The session file stores a pointer to the ledger, not a copy of the ledger.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from xenon.kernel.contracts import (
    Capability,
    Obligation,
    ObligationSource,
    PlanProposal,
    PropositionKind,
    StepContract,
    StepKind,
    ToolIntent,
)
from xenon.ledger.events import Event
from xenon.ledger.projections import project_evidence, project_task_state
from xenon.ledger.store import IntegrityReport, Ledger


def kernel_ledger_dir() -> Path:
    return Path.home() / ".xenon" / "ledger" / "repl"


def _safe_ledger_path(value: str | Path) -> Path | None:
    """Only allow read-only access to a ledger under the kernel ledger root."""

    try:
        root = kernel_ledger_dir().resolve()
        path = Path(value).expanduser().resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError):
        return None
    if path.suffix != ".jsonl" or not path.is_file():
        return None
    return path


@dataclass(frozen=True, slots=True)
class ExtractedPlan:
    """A validated plan read from a planned kernel ledger."""

    task_id: str
    ledger_path: Path
    plan: PlanProposal
    digest: str


@dataclass(frozen=True, slots=True)
class KernelEvidence:
    task_id: str
    ledger_path: Path
    events: tuple[Event, ...]
    task_state: Mapping[str, Any]
    evidence: Mapping[str, Any]
    integrity: IntegrityReport
    result: Mapping[str, Any] | None

    @property
    def result_path(self) -> Path:
        return self.ledger_path.with_suffix(".result.json")


def load_kernel_evidence(value: str | Path) -> KernelEvidence | None:
    path = _safe_ledger_path(value)
    if path is None:
        return None
    task_id = path.stem
    ledger = Ledger(path, task_id)
    events = ledger.read()
    result: Mapping[str, Any] | None = None
    try:
        payload = json.loads(
            path.with_suffix(".result.json").read_text(encoding="utf-8")
        )
        if isinstance(payload, Mapping):
            result = payload
    except (OSError, ValueError):
        result = None
    try:
        task_state = project_task_state(events)
        evidence = project_evidence(events)
    except Exception:  # noqa: BLE001 — a corrupt ledger is still inspectable
        task_state = {}
        evidence = {}
    return KernelEvidence(
        task_id=task_id,
        ledger_path=path,
        events=events,
        task_state=task_state,
        evidence=evidence,
        integrity=ledger.verify(),
        result=result,
    )


def find_kernel_evidence(value: str) -> KernelEvidence | None:
    """Resolve a task id or absolute ledger path below the kernel root."""

    candidate = Path(value).expanduser()
    if candidate.is_file():
        return load_kernel_evidence(candidate)
    return load_kernel_evidence(kernel_ledger_dir() / f"{value}.jsonl")


def current_kernel_evidence(repl: Any) -> KernelEvidence | None:
    """Load the ledger pointed at by the REPL's latest kernel turn."""

    pointer = getattr(repl, "_last_kernel_ledger", None)
    if not pointer:
        return None
    return load_kernel_evidence(pointer)


def kernel_ledger_files(limit: int = 20) -> list[Path]:
    root = kernel_ledger_dir()
    if not root.is_dir():
        return []
    try:
        files = sorted(
            root.glob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    return files[:limit]


def snapshot_for_session(repl: Any) -> dict[str, Any] | None:
    path = _safe_ledger_path(getattr(repl, "_last_kernel_ledger", "") or "")
    if path is None:
        return None
    snapshot: dict[str, Any] = {
        "task_id": path.stem,
        "ledger": str(path),
        "result": str(path.with_suffix(".result.json")),
    }
    approval = _current_approval_for_snapshot(repl, path.stem)
    if approval is not None:
        snapshot["approval"] = approval
    return snapshot


def _current_approval_for_snapshot(
    repl: Any, task_id: str
) -> dict[str, Any] | None:
    """Approval fact to persist with the pointer (never authority by itself)."""

    events = getattr(getattr(repl, "_session_events", None), "read", None)
    live = plan_approval(events() if callable(events) else (), task_id)
    if live is not None:
        return {
            "task_id": task_id,
            "status": live["status"],
            "plan_digest": live["plan_digest"],
            "at": live.get("at"),
            "event_id": live.get("event_id"),
        }
    snapshot = getattr(repl, "_kernel_plan_approval", None)
    if isinstance(snapshot, Mapping) and str(snapshot.get("task_id") or "") == task_id:
        return dict(snapshot)
    return None


def restore_session_pointer(repl: Any, extra: Mapping[str, Any]) -> bool:
    pointer = extra.get("kernel_evidence")
    if not isinstance(pointer, Mapping):
        return False
    path = _safe_ledger_path(str(pointer.get("ledger") or ""))
    if path is None:
        repl._last_kernel_ledger = None
        repl._kernel_plan_approval = None
        return False
    repl._last_kernel_ledger = path
    repl._last_kernel_task_id = path.stem
    approval = pointer.get("approval")
    # Store it without validating digest here: the resolver validates against
    # the ledger at read time, so a stale snapshot cannot grant authority.
    repl._kernel_plan_approval = (
        dict(approval) if isinstance(approval, Mapping) else None
    )
    return True


def _chain_line(report: IntegrityReport) -> str:
    if report.valid:
        return (
            f"链: ✅ 完整 · {report.events} 条事件 · "
            f"head {report.last_hash[:12] or '-'}"
        )
    reason = report.reason or "invalid"
    return f"链: ❌ 无效 · {report.events} 条事件 · {reason}"


def _tool_summary(evidence: Mapping[str, Any]) -> str:
    receipts = list(evidence.get("receipts") or ())
    if not receipts:
        return "🔧 无工具调用"
    names: dict[str, int] = {}
    failed = 0
    for receipt in receipts:
        name = str(receipt.get("tool") or "?")
        names[name] = names.get(name, 0) + 1
        if receipt.get("status") not in {"ok", "OK"}:
            failed += 1
    parts = " · ".join(f"{name} ×{count}" for name, count in sorted(names.items()))
    return f"🔧 {len(receipts)} 次（{parts}）· 失败 {failed}"


def plan_approval(events: Any, task_id: str) -> Mapping[str, Any] | None:
    """Latest plan decision for one kernel task, or None.

    Plan approval is a REPL decision, so it lives in session events rather
    than the append-only kernel ledger.  Only events bound to this task_id
    count; a decision for another task never leaks across turns.
    """

    latest: Mapping[str, Any] | None = None
    for event in events or ():
        if not isinstance(event, Mapping):
            continue
        kind = str(event.get("type") or "")
        if kind not in {"plan/approved", "plan/rejected"}:
            continue
        if str(event.get("task_id") or "") != str(task_id):
            continue
        latest = {
            "status": "approved" if kind == "plan/approved" else "rejected",
            "plan_digest": str(event.get("plan_digest") or ""),
            "at": event.get("at"),
            "event_id": event.get("id"),
        }
    return latest


def planned_plan(evidence: KernelEvidence) -> ExtractedPlan | None:
    """Extract a validated plan only from a planned, intact ledger.

    The kernel ledger is immutable after the plan-only run.  This function
    never executes the plan or grants authority; it only turns the serialized
    plan into the same contract the runtime accepted.
    """

    if not evidence.integrity.valid:
        return None
    result = evidence.result or {}
    state = evidence.task_state
    phase = str(result.get("phase") or state.get("phase") or "")
    if phase != "planned":
        return None
    raw = state.get("plan")
    if not isinstance(raw, Mapping):
        return None
    try:
        proposal = _parse_plan(raw)
    except (TypeError, ValueError, KeyError):
        return None
    digest = _plan_digest(raw)
    return ExtractedPlan(
        task_id=evidence.task_id,
        ledger_path=evidence.ledger_path,
        plan=proposal,
        digest=digest,
    )


def _plan_digest(raw: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def accepted_plan_digest(evidence: KernelEvidence) -> str:
    """Digest of the plan this ledger accepted, regardless of current phase."""

    if not evidence.integrity.valid:
        return ""
    raw = evidence.task_state.get("plan")
    if not isinstance(raw, Mapping):
        return ""
    return _plan_digest(raw)


@dataclass(frozen=True, slots=True)
class SessionApproval:
    """Approval fact persisted with a session pointer.

    It is not authority by itself: restoring it requires the same task id and
    the same accepted-plan digest in the ledger.
    """

    task_id: str
    status: str
    plan_digest: str
    at: Any = None


def _parse_plan(raw: Mapping[str, Any]) -> PlanProposal:
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError("planned plan has no steps")
    steps: list[StepContract] = []
    seen: set[int] = set()
    for item in steps_raw:
        if not isinstance(item, Mapping):
            raise ValueError("plan step is not an object")
        step_id = item.get("step_id", item.get("id"))
        if not isinstance(step_id, int) or isinstance(step_id, bool):
            raise ValueError("plan step id must be an integer")
        if step_id in seen:
            raise ValueError("duplicate plan step id")
        seen.add(step_id)
        depends_on = tuple(int(value) for value in item.get("depends_on") or ())
        if any(value not in seen or value == step_id for value in depends_on):
            raise ValueError("plan dependency is not an earlier step")
        intent_raw = item.get("intent")
        intent = None
        if intent_raw is not None:
            if not isinstance(intent_raw, Mapping):
                raise ValueError("plan intent is not an object")
            intent = ToolIntent(
                capability=Capability(str(intent_raw.get("capability") or "")),
                params=dict(intent_raw.get("params") or {}),
                targets=tuple(
                    str(value) for value in intent_raw.get("targets") or ()
                ),
                prefer=intent_raw.get("prefer"),
            )
        expects_raw = item.get("expects")
        if expects_raw is None:
            expects_raw = []
        if not isinstance(expects_raw, list):
            raise ValueError("plan expects is not a list")
        expects: list[Obligation] = []
        for expect in expects_raw:
            if not isinstance(expect, Mapping):
                raise ValueError("plan expectation is not an object")
            obligation_id = str(expect.get("id") or "")
            if not obligation_id:
                raise ValueError("plan expectation has no id")
            expects.append(
                Obligation(
                    id=obligation_id,
                    kind=PropositionKind(str(expect.get("kind") or "")),
                    target=str(expect.get("target") or ""),
                    expect=dict(expect.get("expect") or {}),
                    source=ObligationSource.PLAN,
                    since_seq=int(expect.get("since_seq") or 0),
                )
            )
        steps.append(
            StepContract(
                id=step_id,
                kind=StepKind(str(item.get("kind") or "")),
                intent=intent,
                targets=tuple(str(value) for value in item.get("targets") or ()),
                expects=tuple(expects),
                depends_on=depends_on,
                prefer=item.get("prefer"),
            )
        )
    return PlanProposal(
        steps=tuple(steps),
        analysis=str(raw.get("analysis") or ""),
    )


def resolve_plan_approval(
    repl: Any, evidence: KernelEvidence
) -> Mapping[str, Any] | None:
    """Approval for this ledger: live session events first, snapshot second.

    A snapshot is only trusted when its task id and plan digest match the
    ledger.  A decision for another task, or for a different plan revision,
    never leaks into this view.
    """

    events = getattr(getattr(repl, "_session_events", None), "read", None)
    live = plan_approval(events() if callable(events) else (), evidence.task_id)
    if live is not None:
        return live
    snapshot = getattr(repl, "_kernel_plan_approval", None)
    if not isinstance(snapshot, Mapping):
        return None
    if str(snapshot.get("task_id") or "") != evidence.task_id:
        return None
    digest = accepted_plan_digest(evidence)
    if not digest or str(snapshot.get("plan_digest") or "") != digest:
        return None
    status = str(snapshot.get("status") or "")
    if status not in {"approved", "rejected"}:
        return None
    return {
        "status": status,
        "plan_digest": digest,
        "at": snapshot.get("at"),
        "event_id": snapshot.get("event_id"),
    }


def plan_lines(evidence: KernelEvidence, *, limit: int = 8) -> list[str]:
    """Render the kernel plan without turning it into a new source of truth."""

    state = evidence.task_state
    plan = state.get("plan")
    steps = list(plan.get("steps") or ()) if isinstance(plan, Mapping) else []
    if not steps:
        return ["🧭 Kernel 计划：（无）"]
    statuses = state.get("steps") if isinstance(state.get("steps"), Mapping) else {}
    lines = [f"🧭 Kernel 计划（{len(steps)} 步）"]
    for step in steps[:limit]:
        if not isinstance(step, Mapping):
            continue
        step_id = step.get("step_id")
        status = "?"
        if isinstance(statuses, Mapping):
            item = statuses.get(str(step_id))
            if isinstance(item, Mapping):
                status = str(item.get("status") or "?")
        kind = str(step.get("kind") or "?")
        targets = [str(item) for item in step.get("targets") or () if item]
        suffix = f" · {', '.join(targets[:3])}" if targets else ""
        lines.append(f"  {step_id}. [{status}] {kind}{suffix}")
    if len(steps) > limit:
        lines.append(f"  … 另有 {len(steps) - limit} 步")
    analysis = (
        " ".join(str(plan.get("analysis") or "").split())
        if isinstance(plan, Mapping)
        else ""
    )
    if analysis:
        lines.append(f"  {analysis[:160]}")
    return lines


def goal_lines(evidence: KernelEvidence) -> list[str]:
    """Compact /goal projection sourced only from one kernel ledger."""

    state = evidence.task_state
    result = evidence.result or {}
    raw_objective = state.get("user_objective") or state.get("objective")
    objective = " ".join(str(raw_objective or "").split())
    lines = [
        f"🎯 Kernel 目标：{objective[:200] if objective else '（无）'}",
        f"🧭 阶段：{state.get('phase', '—')} · "
        f"accepted: {result.get('accepted', '?')} · "
        f"evidence: {result.get('evidence_status', '—')}",
    ]
    lines.extend(plan_lines(evidence, limit=6))
    debt = list(state.get("debt") or ())
    if debt:
        lines.append(f"⚠️ 未结债务 {len(debt)}:")
        for item in debt[:5]:
            if isinstance(item, Mapping):
                lines.append(
                    f"   {item.get('obligation_id') or '?'}: "
                    f"{str(item.get('reason') or '')[:90]}"
                )
    artifacts = list(state.get("artifacts") or ())
    if artifacts:
        lines.append("📦 产物：" + ", ".join(str(item) for item in artifacts[:8]))
    finalized = state.get("finalized")
    if isinstance(finalized, Mapping):
        lines.append(
            f"🏁 finalized: commit={finalized.get('commit') or '-'} "
            f"evidence={finalized.get('evidence_status') or '-'}"
        )
    if not evidence.integrity.valid:
        lines.append("⚠️ 链无效：以上投影仅供取证，不可作为可信交付。")
    return lines


def _human_view(evidence: KernelEvidence) -> str:
    state = evidence.task_state
    result = evidence.result or {}
    lines = [
        f"🔗 Kernel evidence · {evidence.task_id}",
        f"文件: {evidence.ledger_path}",
        _chain_line(evidence.integrity),
        "",
        f"阶段: {state.get('phase', '—')} · "
        f"accepted: {result.get('accepted', '?')} · "
        f"evidence: {result.get('evidence_status', '—')}",
    ]
    raw_objective = state.get("user_objective") or state.get("objective")
    objective = str(raw_objective or "").strip()
    if objective:
        lines.append(f"🎯 目标: {objective[:160]}")
    lines.append(f"   {_tool_summary(evidence.evidence)}")

    tests = list(state.get("tests") or ())
    if tests:
        lines.append("🧪 测试:")
        for test in tests[-5:]:
            lines.append(
                f"   {test.get('target', '?')} · "
                f"{test.get('passed', 0)} passed / {test.get('failed', 0)} failed"
            )
    artifacts = list(state.get("artifacts") or ())
    if artifacts:
        lines.append("📦 产物: " + ", ".join(str(item) for item in artifacts[:8]))
    commits = list(state.get("commits") or ())
    if commits:
        lines.append(
            "🧾 提交: "
            + ", ".join(str(item.get("sha") or "?") for item in commits[-3:])
        )
    claims = list(evidence.evidence.get("claims") or ())
    resolutions = list(evidence.evidence.get("resolutions") or ())
    if claims or resolutions:
        lines.append(f"📌 Claims: {len(claims)} · resolutions: {len(resolutions)}")
        for resolution in resolutions[:5]:
            status = str(resolution.get("status") or "?")
            claim_id = str(resolution.get("claim_id") or "?")
            reason = str(resolution.get("reason") or "")
            lines.append(f"   {claim_id}: {status} {reason[:80]}")
    audits = list(evidence.evidence.get("audit") or ())
    if audits:
        audit = next(
            (
                item
                for item in reversed(audits)
                if item.get("scope") == "final"
            ),
            audits[-1],
        )
        coverage = list(audit.get("coverage") or ())
        if coverage:
            counts: dict[str, int] = {}
            for row in coverage:
                status = str(row.get("status") or "?")
                counts[status] = counts.get(status, 0) + 1
            summary = " · ".join(
                f"{name} {count}" for name, count in sorted(counts.items())
            )
            lines.append(f"🧮 V2 覆盖：{len(coverage)} 项（{summary}）")
            for row in coverage:
                if row.get("status") == "bound":
                    continue
                lines.append(
                    f"   {row.get('obligation_id') or '?'}: "
                    f"{row.get('status') or '?'} "
                    f"{str(row.get('reason') or '')[:90]}"
                )
        gaps = list(audit.get("gaps") or ())
        for gap in gaps[:5]:
            lines.append(
                f"   gap {gap.get('obligation_id') or '?'}: "
                f"{str(gap.get('next_action') or gap.get('reason') or '')[:90]}"
            )
    debt = list(evidence.evidence.get("debt") or ())
    if debt:
        lines.append(f"⚠️ 未满足证据 {len(debt)}:")
        for item in debt[:5]:
            lines.append(
                f"   {item.get('obligation_id') or '?'}: "
                f"{str(item.get('reason') or '')[:90]}"
            )
    delivered = state.get("delivered")
    if isinstance(delivered, Mapping):
        answer = str(delivered.get("answer") or "").replace("\n", " ").strip()
        if answer:
            lines.append(f"📤 {answer[:160]}")
    finalized = state.get("finalized")
    if isinstance(finalized, Mapping):
        lines.append(
            f"🏁 finalized: commit={finalized.get('commit') or '-'} "
            f"evidence={finalized.get('evidence_status') or '-'}"
        )
    if not evidence.integrity.valid:
        lines.append("⚠️ 链无效：以上投影仅供取证，不可作为可信交付。")
    return "\n".join(lines)


def _raw_view(evidence: KernelEvidence) -> str:
    report = evidence.integrity
    lines = [
        f"🔗 Kernel evidence（raw）· {evidence.task_id}",
        f"文件: {evidence.ledger_path}",
        _chain_line(report),
        f"事件数: {len(evidence.events)} · last_seq: {report.last_seq}",
        f"task_state keys: {', '.join(sorted(evidence.task_state)) or '-'}",
        "",
    ]
    for event in evidence.events:
        payload = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
        lines.append(
            f"{event.seq:>3} {event.kind.value:<18} "
            f"h={event.hash[:10]} prev={event.prev_hash[:10] or '-'} "
            f"{payload[:240]}"
        )
    return "\n".join(lines)


def render_kernel_evidence(evidence: KernelEvidence, *, raw: bool = False) -> str:
    return _raw_view(evidence) if raw else _human_view(evidence)


__all__ = [
    "ExtractedPlan",
    "KernelEvidence",
    "current_kernel_evidence",
    "find_kernel_evidence",
    "goal_lines",
    "kernel_ledger_dir",
    "kernel_ledger_files",
    "load_kernel_evidence",
    "SessionApproval",
    "accepted_plan_digest",
    "plan_approval",
    "plan_lines",
    "planned_plan",
    "render_kernel_evidence",
    "resolve_plan_approval",
    "restore_session_pointer",
    "snapshot_for_session",
]
