"""Model-facing projection.

This is the #196 minimum: a deterministic view over the task, catalog and the
current ledger prefix.  #197 will grow the prompt builder, token budget and
lazy ``result_id`` reads; the runtime only depends on this function's contract.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

from xenon.kernel.contracts import (
    Budget,
    Debt,
    ModelView,
    TaskPhase,
    TaskSpec,
    ToolSpec,
)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _tool_schema(spec: ToolSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": dict(spec.params_schema),
        "capabilities": sorted(item.value for item in spec.capabilities),
    }


def _working_set(events: Sequence[Any], limit: int = 12) -> tuple[dict[str, Any], ...]:
    recent = events[-limit:]
    return tuple(
        {
            "kind": event.kind.value,
            "payload": dict(event.payload),
        }
        for event in recent
    )


def build_model_view(
    *,
    spec: TaskSpec,
    catalog: Any,
    ledger: Any,
    debt: Sequence[Debt] = (),
    budget: Budget | None = None,
    phase: TaskPhase = TaskPhase.RUNNING,
) -> ModelView:
    """Build the deterministic view a strategy is allowed to see."""

    tools = tuple(_tool_schema(item) for item in catalog.specs())
    working_set = _working_set(tuple(ledger.read()))
    budget = budget or spec.budget
    constraints = "\n".join(f"- {item}" for item in spec.constraints)
    debt_lines = "\n".join(
        f"- {item.obligation.id}: {item.reason}" for item in debt
    )
    system = (
        "You are Xenon's coding strategy.  Propose one action at a time; "
        "the harness executes it and verifies the effects.\n"
        f"Objective: {spec.objective}\n"
        f"Workspace: {spec.workspace}\n"
        f"Mode: {spec.mode.value}\n"
        f"Phase: {phase.value}\n"
    )
    if constraints:
        system += f"Constraints:\n{constraints}\n"
    if debt_lines:
        system += f"Open obligations:\n{debt_lines}\n"

    payload = {
        "system": system,
        "tools": list(tools),
        "working_set": list(working_set),
        "debt": [item.obligation.id for item in debt],
        "budget": {
            "max_tokens": budget.max_tokens,
            "max_wall_seconds": budget.max_wall_seconds,
            "max_steps": budget.max_steps,
        },
        "input_seq": ledger.watermark(),
        "phase": phase.value,
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
    )
