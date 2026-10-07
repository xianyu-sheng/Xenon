"""Fold a session event log into a read-only view.

The view is the restrained replacement for a graph engine: goals, open gates,
artifacts and recent turns are *projections* of durable events, never a
second source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Goal:
    objective: str
    status: str  # active | paused | closed
    at: float


@dataclass
class Artifact:
    path: str
    tool: str
    at: float


@dataclass
class OpenGate:
    gate_id: str
    kind: str
    reason: str
    at: float


@dataclass
class SessionView:
    goals: list[Goal] = field(default_factory=list)
    open_gates: list[OpenGate] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    recent_users: list[str] = field(default_factory=list)
    assistant_previews: list[str] = field(default_factory=list)

    @property
    def active_goal(self) -> Goal | None:
        for goal in reversed(self.goals):
            if goal.status == "active":
                return goal
        return None


def project(events: list[dict]) -> SessionView:
    """Deterministically fold *events* into a :class:`SessionView`."""

    view = SessionView()
    open_gate_ids: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        at = float(event.get("at") or 0.0)

        if kind == "goal/opened":
            objective = str(event.get("objective") or "").strip()
            if objective:
                for goal in view.goals:
                    if goal.status == "active":
                        goal.status = "paused"
                view.goals.append(Goal(objective=objective, status="active", at=at))
        elif kind == "goal/paused":
            for goal in reversed(view.goals):
                if goal.status == "active":
                    goal.status = "paused"
                    break
        elif kind == "goal/closed":
            for goal in reversed(view.goals):
                if goal.status == "active":
                    goal.status = "closed"
                    break
        elif kind == "gate/opened":
            gate_id = str(event.get("gate_id") or event.get("id") or "")
            if gate_id:
                open_gate_ids.add(gate_id)
                view.open_gates.append(
                    OpenGate(
                        gate_id=gate_id,
                        kind=str(event.get("kind") or "approval"),
                        reason=str(event.get("reason") or ""),
                        at=at,
                    )
                )
        elif kind == "gate/resolved":
            gate_id = str(event.get("gate_id") or "")
            open_gate_ids.discard(gate_id)
            view.open_gates = [
                gate for gate in view.open_gates if gate.gate_id != gate_id
            ]
        elif kind == "tool/result" and event.get("success"):
            for path in event.get("paths") or []:
                if path:
                    view.artifacts.append(
                        Artifact(path=str(path), tool=str(event.get("tool") or ""), at=at)
                    )
        elif kind == "turn/user":
            text = str(event.get("text") or "").strip()
            if text:
                view.recent_users.append(text[:200])
        elif kind == "turn/assistant":
            preview = str(event.get("preview") or "").strip()
            if preview:
                view.assistant_previews.append(preview[:200])
    return view
