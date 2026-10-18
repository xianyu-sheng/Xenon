"""Persistent task-plan tool (Codex-style ``update_plan``).

The plan is execution state, not an evidence gate: it lives in
``AgentContext`` and is echoed back to the model as an observation.  It gives
long ReAct runs an explicit "what is done / what is in progress" anchor,
instead of relying on the model to remember a plan in prose.
"""

from __future__ import annotations

import json
from typing import Any

_VALID_STATUSES = {"pending", "in_progress", "completed"}
_STATUS_ALIASES = {
    "todo": "pending",
    "not_started": "pending",
    "in-progress": "in_progress",
    "inprogress": "in_progress",
    "active": "in_progress",
    "done": "completed",
    "complete": "completed",
    "finished": "completed",
}


def _normalize_status(value: Any) -> str:
    status = str(value or "pending").strip().lower().replace(" ", "_")
    status = _STATUS_ALIASES.get(status, status)
    return status if status in _VALID_STATUSES else "pending"


def normalize_plan_steps(raw: Any) -> list[dict[str, str]]:
    """Normalize caller-supplied steps without inventing content."""

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            # A single-step string is accepted; malformed JSON is not.
            return [{"step": raw.strip(), "status": "pending"}] if raw.strip() else []
    if isinstance(raw, dict):
        raw = raw.get("steps", [])
    if not isinstance(raw, list):
        return []

    steps: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            text, status = item, "pending"
        elif isinstance(item, dict):
            text = (
                item.get("step")
                or item.get("task")
                or item.get("title")
                or item.get("description")
                or ""
            )
            status = item.get("status", "pending")
        else:
            continue
        text = str(text).strip()
        if text:
            steps.append({"step": text, "status": _normalize_status(status)})
    return steps


def format_plan(steps: list[dict[str, str]]) -> str:
    """Render a compact, model-readable checklist."""

    marks = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
    return "\n".join(f"- {marks.get(s['status'], '[ ]')} {s['step']}" for s in steps)


class PlanToolsMixin:
    """Mixin providing the built-in ``update_plan`` action."""

    def _update_plan(self, context: Any) -> dict[str, Any]:
        steps = normalize_plan_steps(getattr(self, "steps", None))
        if not steps:
            return {
                "action_type": "update_plan",
                "success": False,
                "content": "",
                "error": "update_plan 需要非空 steps 数组",
            }
        in_progress = sum(1 for step in steps if step["status"] == "in_progress")
        if in_progress > 1:
            return {
                "action_type": "update_plan",
                "success": False,
                "content": "",
                "error": "update_plan 最多只能有一个 in_progress 步骤",
            }

        plan = {
            "steps": steps,
            "explanation": str(getattr(self, "explanation", "") or "").strip(),
        }
        if context is not None and hasattr(context, "set"):
            context.set("_agent_plan", plan)
            context.set("_agent_plan_text", format_plan(steps))
        text = "计划已更新：\n" + format_plan(steps)
        if plan["explanation"]:
            text += f"\n说明：{plan['explanation']}"
        return {
            "action_type": "update_plan",
            "success": True,
            "content": text,
            "plan": plan,
        }
