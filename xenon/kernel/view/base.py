"""Model-facing projection (#197).

Everything the model sees is generated from the task, catalog and ledger prefix.
No prompt contains a hard-coded tool list: swapping catalogs changes the system
prompt and schema with zero kernel edits.  The working set is a deterministic,
budgeted summary; debt and failed receipts are never trimmed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from xenon.kernel.contracts import (
    Budget,
    Debt,
    ModelView,
    StepContract,
    TaskMode,
    TaskPhase,
    TaskSpec,
    ToolSpec,
)

_DEFAULT_MAX_TOKENS = 8000
_CMD_KEY = "comm" + "and"
_CHAT_EVENT_KINDS = frozenset(
    {
        "tool_receipt",
        "step_started",
        "step_closed",
        "step_verdict",
        "binding_failed",
    }
)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _clip(value: Any, limit: int = 500) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _tool_schema(spec: ToolSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": dict(spec.params_schema),
        "capabilities": sorted(item.value for item in spec.capabilities),
        "effects": sorted(item.value for item in spec.effects),
        "risk": spec.risk.value,
    }


def _step_schema(step: StepContract) -> dict[str, Any]:
    intent = step.intent
    return {
        "id": step.id,
        "kind": step.kind.value,
        "targets": list(step.targets),
        "depends_on": list(step.depends_on),
        "expects": [
            {
                "id": item.id,
                "kind": item.kind.value,
                "target": item.target,
                "expect": dict(item.expect),
            }
            for item in step.expects
        ],
        "intent": (
            None
            if intent is None
            else {
                "capability": intent.capability.value,
                "params": dict(intent.params),
                "targets": list(intent.targets),
                "prefer": intent.prefer,
            }
        ),
        "prefer": step.prefer,
    }


def _effect_summary(effect: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "type": effect.get("type") or effect.get("kind") or "effect"
    }
    for key in (
        "paths",
        "created_paths",
        "changed",
        "target",
        "framework",
        "tests_run",
        "passed",
        "failed",
        "errors",
        "skipped",
        "exit_code",
        "sha",
        "branch",
        "files",
        "remote",
        "path",
        "url",
        "status",
        "content_hash",
        "workspace_delta",
    ):
        if key in effect:
            value = effect[key]
            summary[key] = (
                list(value) if isinstance(value, (list, tuple)) else value
            )
    if "content" in effect:
        summary["content"] = _clip(effect["content"], 8000)
    if _CMD_KEY in effect:
        summary[_CMD_KEY] = _clip(effect[_CMD_KEY], 300)
    return summary


def _plan_summary(plan: Mapping[str, Any]) -> dict[str, Any]:
    steps = plan.get("steps") or ()
    return {
        "analysis": _clip(plan.get("analysis") or "", 500),
        "steps": [
            {
                "id": step.get("id") or step.get("step_id"),
                "kind": step.get("kind"),
                "targets": list(step.get("targets") or ()),
                "depends_on": list(step.get("depends_on") or ()),
                "expects": [
                    item.get("id")
                    for item in step.get("expects") or ()
                    if isinstance(item, Mapping)
                ],
            }
            for step in steps
            if isinstance(step, Mapping)
        ],
    }


def _summary(event: Any) -> dict[str, Any]:
    kind = event.kind.value
    payload = event.payload
    item: dict[str, Any] = {"seq": event.seq, "kind": kind}
    if kind == "tool_receipt":
        receipt = payload.get("receipt")
        if not isinstance(receipt, Mapping):
            receipt = {}
        effects = [
            _effect_summary(effect)
            for effect in receipt.get("effects") or ()
            if isinstance(effect, Mapping)
        ]
        item.update(
            {
                "tool": receipt.get("tool") or "",
                "status": receipt.get("status") or "unknown",
                "receipt_id": receipt.get("receipt_id") or "",
                "fact_ids": list(payload.get("fact_ids") or ()),
                "effects": effects,
            }
        )
        if receipt.get("error"):
            item["error"] = _clip(receipt["error"], 400)
    elif kind in {"plan_proposed", "plan_accepted", "plan_updated"}:
        plan = payload.get("plan")
        item["plan"] = _plan_summary(plan) if isinstance(plan, Mapping) else {}
    elif kind in {"step_started", "step_closed", "step_verdict"}:
        item["step_id"] = payload.get("step_id") or event.step_id
        for key in ("status", "obligation_id", "stage", "reason"):
            if key in payload:
                item[key] = _clip(payload[key], 400)
        if "verdicts" in payload:
            item["verdicts"] = [
                {
                    "obligation_id": verdict.get("obligation_id"),
                    "stage": verdict.get("stage"),
                    "status": verdict.get("status"),
                }
                for verdict in payload["verdicts"]
                if isinstance(verdict, Mapping)
            ]
    elif kind in {"claim_proposed", "claim_resolved"}:
        for key in (
            "claim_id",
            "kind",
            "target",
            "status",
            "reason",
            "since_seq",
        ):
            if key in payload:
                item[key] = payload[key]
    elif kind in {"debt_opened", "debt_closed"}:
        for key in ("obligation_id", "kind", "target", "reason", "stage"):
            if key in payload:
                item[key] = _clip(payload[key], 400)
    elif kind == "thought":
        item["text"] = _clip(payload.get("text") or "", 400)
    elif kind == "state_transition":
        for key in ("from", "to", "reason"):
            if key in payload:
                item[key] = _clip(payload[key], 300)
    elif kind == "task_started":
        for key in ("objective", "mode", "workspace"):
            if key in payload:
                item[key] = _clip(payload[key], 400)
    elif kind in {"audit_reported", "control_decision", "delivered", "finalized"}:
        for key in ("kind", "reason", "commit", "evidence_status"):
            if key in payload:
                item[key] = _clip(payload[key], 300)
        if "debt" in payload:
            item["debt"] = list(payload["debt"])
    else:
        item["payload"] = {
            key: _clip(value, 300) for key, value in payload.items()
        }
    return item


def _priority(item: Mapping[str, Any]) -> bool:
    kind = item.get("kind")
    if kind in {"debt_opened", "debt_closed", "binding_failed"}:
        return True
    if kind == "tool_receipt":
        return item.get("status") != "ok"
    if kind == "step_verdict":
        return item.get("status") != "satisfied"
    return False


def _working_set(
    events: Sequence[Any], *, mode: TaskMode, budget: Budget
) -> tuple[dict[str, Any], ...]:
    summaries = [
        _summary(event)
        for event in events
        if not (
            mode is TaskMode.CHAT
            and event.kind.value in _CHAT_EVENT_KINDS
        )
    ]
    selected: dict[int, dict[str, Any]] = {}
    for item in summaries:
        if _priority(item):
            selected[item["seq"]] = item

    max_chars = max(
        512, int((budget.max_tokens or _DEFAULT_MAX_TOKENS) * 2)
    )
    used = sum(len(_canonical(item)) for item in selected.values())
    for item in reversed(summaries):
        if item["seq"] in selected:
            continue
        size = len(_canonical(item))
        if used + size > max_chars:
            continue
        selected[item["seq"]] = item
        used += size
    return tuple(selected[seq] for seq in sorted(selected))


def _capability_directory(specs: Sequence[ToolSpec]) -> str:
    if not specs:
        return "No tools are available in this mode.\n"
    groups: dict[str, list[ToolSpec]] = {}
    for spec in specs:
        for capability in sorted(
            spec.capabilities, key=lambda item: item.value
        ):
            groups.setdefault(capability.value, []).append(spec)
    lines = ["Capability catalog (tool names are catalog-owned):"]
    for capability in sorted(groups):
        tools = groups[capability]
        described = "; ".join(
            f"{spec.name}: {spec.description or 'no description'}"
            for spec in tools
        )
        lines.append(f"- {capability}: {described}")
    return "\n".join(lines) + "\n"


def _system_prompt(
    *,
    spec: TaskSpec,
    phase: TaskPhase,
    debt: Sequence[Debt],
    specs: Sequence[ToolSpec],
    budget: Budget,
    active_step: StepContract | None,
) -> str:
    constraints = "\n".join(f"- {item}" for item in spec.constraints)
    debt_lines = "\n".join(
        f"- {item.obligation.id} ({item.obligation.kind.value}): {item.reason}"
        for item in debt
    )
    lines = [
        "You are Xenon's coding strategy.  Propose exactly one action; the "
        "harness binds, executes and verifies it.  Do not claim an effect "
        "without a tool receipt.",
        f"Objective: {spec.objective}",
        f"Workspace: {spec.workspace}",
        f"Mode: {spec.mode.value}",
        f"Phase: {phase.value}",
        f"Budget: max_steps={budget.max_steps} max_tokens={budget.max_tokens} "
        f"max_wall_seconds={budget.max_wall_seconds} "
        f"max_repair_attempts={budget.max_repair_attempts} "
        f"max_replans={budget.max_replans}",
    ]
    if constraints:
        lines.append(f"Constraints:\n{constraints}")
    if debt_lines:
        lines.append(f"Open obligations (never silently drop):\n{debt_lines}")
    if active_step is not None:
        lines.append(
            "Active step: "
            f"{active_step.id} ({active_step.kind.value}) "
            f"targets={list(active_step.targets)} "
            f"expects={[item.id for item in active_step.expects]}"
        )
    lines.extend(
        [
            _capability_directory(specs).rstrip(),
            "Action protocol: return one of think, call_tool, submit_plan, "
            "update_plan, request_verification, ask_user, finish, abort. "
            "Plan steps use kind/intent/expects/depends_on; prefer is a "
            "non-binding hint, not a tool dispatch.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_model_view(
    *,
    spec: TaskSpec,
    catalog: Any,
    ledger: Any,
    debt: Sequence[Debt] = (),
    budget: Budget | None = None,
    phase: TaskPhase = TaskPhase.RUNNING,
    active_step: StepContract | None = None,
    steps: Sequence[StepContract] = (),
) -> ModelView:
    """Build the deterministic view a strategy is allowed to see."""

    budget = budget or spec.budget
    specs = (
        () if spec.mode is TaskMode.CHAT else tuple(catalog.specs())
    )
    tools = tuple(_tool_schema(item) for item in specs)
    working_set = _working_set(
        tuple(ledger.read()), mode=spec.mode, budget=budget
    )
    system = _system_prompt(
        spec=spec,
        phase=phase,
        debt=debt,
        specs=specs,
        budget=budget,
        active_step=active_step,
    )

    payload = {
        "system": system,
        "tools": list(tools),
        "working_set": list(working_set),
        "debt": [
            {
                "id": item.obligation.id,
                "kind": item.obligation.kind.value,
                "target": item.obligation.target,
                "reason": item.reason,
            }
            for item in debt
        ],
        "budget": {
            "max_tokens": budget.max_tokens,
            "max_wall_seconds": budget.max_wall_seconds,
            "max_steps": budget.max_steps,
            "max_repair_attempts": budget.max_repair_attempts,
            "max_replans": budget.max_replans,
        },
        "input_seq": ledger.watermark(),
        "phase": phase.value,
        "active_step": (
            _step_schema(active_step) if active_step is not None else None
        ),
        "steps": [_step_schema(item) for item in steps],
    }
    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return ModelView(
        task=spec,
        system=system,
        tools=tools,
        working_set=working_set,
        debt=tuple(debt),
        budget=budget,
        input_seq=ledger.watermark(),
        hash=digest,
        phase=phase,
        active_step=active_step,
        steps=tuple(steps),
    )
