"""Read-only views over kernel ledgers for /evidence and /resume (#236).

Kernel ledgers are the authoritative facts for kernel turns.  This module only
reads them: it never replays a task, restores an approval, or mutates state.
The session file stores a pointer to the ledger, not a copy of the ledger.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

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
    return {
        "task_id": path.stem,
        "ledger": str(path),
        "result": str(path.with_suffix(".result.json")),
    }


def restore_session_pointer(repl: Any, extra: Mapping[str, Any]) -> bool:
    pointer = extra.get("kernel_evidence")
    if not isinstance(pointer, Mapping):
        return False
    path = _safe_ledger_path(str(pointer.get("ledger") or ""))
    if path is None:
        repl._last_kernel_ledger = None
        return False
    repl._last_kernel_ledger = path
    repl._last_kernel_task_id = path.stem
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
    objective = " ".join(str(state.get("objective") or "").split())
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
    objective = str(state.get("objective") or "").strip()
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
    "KernelEvidence",
    "current_kernel_evidence",
    "find_kernel_evidence",
    "goal_lines",
    "kernel_ledger_dir",
    "kernel_ledger_files",
    "load_kernel_evidence",
    "plan_lines",
    "render_kernel_evidence",
    "restore_session_pointer",
    "snapshot_for_session",
]
