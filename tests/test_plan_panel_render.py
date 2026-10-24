"""Deterministic coverage for the live plan panel renderer (#174)."""

from __future__ import annotations

from types import SimpleNamespace

from rich.text import Text

from xenon.engine.context import AgentContext
from xenon.engine.registry import EngineSpec
from xenon.engine.task_state import TaskState
from xenon.repl.engine_host import EngineHostMixin


class _Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, value=None, *args, **kwargs) -> None:
        if isinstance(value, Text):
            self.lines.append(value.plain)
        elif value is not None:
            self.lines.append(str(value))


class _Bus:
    def __init__(self) -> None:
        self.handlers: list = []

    def subscribe(self, _topic: str, handler) -> None:
        self.handlers.append(handler)


def _host(state: TaskState):
    host = EngineHostMixin()
    host.agent_context = AgentContext({"_task_state": state.to_dict()})
    host._last_plan_signature = None
    return host


def _spec() -> EngineSpec:
    return EngineSpec(name="plan-execute", result_title="Plan-Execute 结果")


def test_final_plan_panel_uses_task_state_and_shows_delivery_gap(monkeypatch) -> None:
    import xenon.repl.engine_host as engine_host

    console = _Console()
    monkeypatch.setattr(engine_host, "console", console)
    state = TaskState(
        objective="implement",
        intent="write_code",
        execution_level=3,
        required=["verify", "commit"],
        steps=[
            {"id": 1, "step": "write source", "status": "completed"},
            {"id": 2, "step": "verify tests", "status": "running"},
        ],
    )

    _host(state)._render_plan_state(_spec())

    rendered = "\n".join(console.lines)
    assert "Plan v1" in rendered
    assert "write source" in rendered
    assert "verify tests" in rendered
    assert "交付缺口" in rendered


def test_live_plan_lines_are_deduplicated_by_step_signature(monkeypatch) -> None:
    import xenon.repl.engine_host as engine_host

    console = _Console()
    monkeypatch.setattr(engine_host, "console", console)
    host = _host(TaskState(objective="x"))
    bus = _Bus()
    host._subscribe_plan_display(SimpleNamespace(event_bus=bus), _spec())
    assert bus.handlers

    snapshot = {
        "plan_version": 2,
        "steps": [{"id": 1, "step": "write", "status": "running"}],
    }
    bus.handlers[0]("Plan v2", snapshot)
    bus.handlers[0]("Plan v2", snapshot)  # duplicate signature: no second line
    snapshot2 = {
        "plan_version": 2,
        "steps": [{"id": 1, "step": "write", "status": "completed"}],
    }
    bus.handlers[0]("Plan v2", snapshot2)

    assert len(console.lines) == 2
    assert "执行: write" in console.lines[0]
    assert "计划 v2 已更新" in console.lines[1]


def test_final_panel_rebuilds_from_ledger_when_context_has_no_task_state(
    monkeypatch,
) -> None:
    import xenon.repl.engine_host as engine_host
    from xenon.engine.evidence_runtime import (
        EventKind,
        EvidenceLedger,
        EvidenceSource,
        LifecyclePhase,
    )

    console = _Console()
    monkeypatch.setattr(engine_host, "console", console)
    ledger = EvidenceLedger("readonly-plan")
    ledger.append(
        LifecyclePhase.PLANNING,
        EventKind.PLAN,
        EvidenceSource.ENGINE,
        {
            "version": 1,
            "steps": [
                {"id": 1, "task": "read README", "status": "completed"},
                {"id": 2, "task": "summarize", "status": "completed"},
            ],
        },
    )
    host = EngineHostMixin()
    host.agent_context = AgentContext({"_evidence_ledger": ledger})
    host._last_plan_signature = None

    host._render_plan_state(_spec())

    rendered = "\n".join(console.lines)
    assert "Plan v1" in rendered
    assert "read README" in rendered
    assert "summarize" in rendered
