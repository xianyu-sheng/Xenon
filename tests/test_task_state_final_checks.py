"""Final delivery checks consume real tool outcomes, not optimistic state."""

from __future__ import annotations

from xenon.engine.callbacks import EngineCallback
from xenon.engine.context import AgentContext
from xenon.engine.result import TerminationReason
from xenon.engine.task_runtime import run_with_delivery
from xenon.engine.task_state import TaskState
from xenon.engine.tool_tracker import ToolExecutionTracker


class _EngineWithTestRuns:
    def __init__(self) -> None:
        self.callback = EngineCallback()
        self._last_tracker = ToolExecutionTracker()
        self._last_termination_reason = None
        self.calls = 0

    def run(self, user_input: str, context=None, ctx_mgr=None) -> str:
        self.calls += 1
        if self.calls == 1:
            self._last_tracker.record(
                "command",
                {"command": "pytest -q"},
                False,
                "1 failed, 2 passed",
                metadata={"exit_code": 1},
            )
        else:
            self._last_tracker.record(
                "command",
                {"command": "pytest -q"},
                True,
                "2 passed",
                metadata={"exit_code": 0},
            )
        return "done"


def test_failed_test_invalidates_prior_pass_and_forces_retry() -> None:
    engine = _EngineWithTestRuns()
    context = AgentContext(
        {
            "_task_state": TaskState(
                objective="implement",
                intent="write_code",
                execution_level=3,
                artifacts=["src/app.py"],
                tests=["pytest -q"],
                required=["verify"],
            ).to_dict()
        }
    )

    result = run_with_delivery(engine, "finish", context, max_delivery_retries=1)

    assert result == "done"
    assert result.reason is TerminationReason.FINAL
    assert engine.calls == 2
    assert context.get("_task_state")["status"] == "completed"
    assert context.get("_task_state")["tests"] == ["pytest -q"]
