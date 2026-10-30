"""Legal task-phase transitions.

The table is the contract; only ``LoopRuntime`` may apply it.  A projection that
shows an impossible transition indicates a bug in the projection, not a second
state machine.
"""

from __future__ import annotations

from typing import Mapping

from xenon.kernel.contracts import TaskPhase

LEGAL_TRANSITIONS: Mapping[TaskPhase, frozenset[TaskPhase]] = {
    TaskPhase.CREATED: frozenset({TaskPhase.INTAKE, TaskPhase.ABORTED}),
    TaskPhase.INTAKE: frozenset(
        {TaskPhase.RUNNING, TaskPhase.BLOCKED, TaskPhase.ABORTED}
    ),
    TaskPhase.RUNNING: frozenset(
        {
            TaskPhase.THINKING,
            TaskPhase.TOOL_CALL,
            TaskPhase.RECONCILING,
            TaskPhase.REPAIRING,
            TaskPhase.REPLANNING,
            TaskPhase.AUDITING,
            TaskPhase.PLANNED,
            TaskPhase.BLOCKED,
            TaskPhase.ABORTED,
            TaskPhase.FAILED,
        }
    ),
    TaskPhase.THINKING: frozenset(
        {
            TaskPhase.RUNNING,
            TaskPhase.TOOL_CALL,
            TaskPhase.AUDITING,
            TaskPhase.ABORTED,
            TaskPhase.FAILED,
        }
    ),
    TaskPhase.TOOL_CALL: frozenset(
        {TaskPhase.RECONCILING, TaskPhase.ABORTED, TaskPhase.FAILED}
    ),
    TaskPhase.RECONCILING: frozenset(
        {
            TaskPhase.RUNNING,
            TaskPhase.REPAIRING,
            TaskPhase.REPLANNING,
            TaskPhase.AUDITING,
            TaskPhase.BLOCKED,
            TaskPhase.ABORTED,
            TaskPhase.FAILED,
        }
    ),
    TaskPhase.REPAIRING: frozenset(
        {
            TaskPhase.RUNNING,
            TaskPhase.REPLANNING,
            TaskPhase.BLOCKED,
            TaskPhase.ABORTED,
            TaskPhase.FAILED,
        }
    ),
    TaskPhase.REPLANNING: frozenset(
        {
            TaskPhase.RUNNING,
            TaskPhase.BLOCKED,
            TaskPhase.ABORTED,
            TaskPhase.FAILED,
        }
    ),
    TaskPhase.AUDITING: frozenset(
        {
            TaskPhase.DELIVERING,
            TaskPhase.REPAIRING,
            TaskPhase.REPLANNING,
            TaskPhase.BLOCKED,
            TaskPhase.ABORTED,
            TaskPhase.FAILED,
        }
    ),
    TaskPhase.DELIVERING: frozenset(
        {
            TaskPhase.DELIVERED,
            TaskPhase.BLOCKED,
            TaskPhase.ABORTED,
            TaskPhase.FAILED,
        }
    ),
    TaskPhase.DELIVERED: frozenset(
        {TaskPhase.FINALIZED, TaskPhase.ABORTED, TaskPhase.FAILED}
    ),
    TaskPhase.PLANNED: frozenset({TaskPhase.RUNNING}),
    TaskPhase.FINALIZED: frozenset(),
    TaskPhase.BLOCKED: frozenset(),
    TaskPhase.ABORTED: frozenset(),
    TaskPhase.FAILED: frozenset(),
}

TERMINAL_PHASES = frozenset(
    {
        TaskPhase.FINALIZED,
        TaskPhase.BLOCKED,
        TaskPhase.ABORTED,
        TaskPhase.FAILED,
    }
)


def is_legal_transition(source: TaskPhase, target: TaskPhase) -> bool:
    return target in LEGAL_TRANSITIONS.get(source, frozenset())


def assert_transition(source: TaskPhase, target: TaskPhase) -> None:
    if not is_legal_transition(source, target):
        raise ValueError(f"illegal task transition: {source.value} -> {target.value}")
