"""Engine results distinguish normal completion from loop/delivery failures."""

from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.engine.react_engine import ReActEngine
from xenon.engine.result import AgentResult, TerminationReason
from xenon.engine.task_state import TaskState


def test_plain_engine_run_returns_final_agent_result():
    engine = ReActEngine(["fake/model"], max_iterations=1)
    engine.run = lambda *args, **kwargs: "done"  # type: ignore[method-assign]

    result = engine.run_task("say done", context=AgentContext({}))

    assert isinstance(result, AgentResult)
    assert result == "done"
    assert result.reason is TerminationReason.FINAL
    assert result.ok is True
    assert engine.last_result is result


def test_missing_delivery_is_not_ok():
    engine = ReActEngine(["fake/model"], max_iterations=1)
    engine.run = lambda *args, **kwargs: "claimed done"  # type: ignore[method-assign]
    context = AgentContext()
    state = TaskState(
        objective="implement #1",
        intent="write_code",
        execution_level=3,
        required=["verify", "commit"],
    )
    context.set("_task_state", state.to_dict())

    result = engine.run_task("finish", context=context, max_delivery_retries=0)

    assert "任务交付未完成" in result
    assert result.reason is TerminationReason.DELIVERY_INCOMPLETE
    assert result.ok is False
    assert set(result.missing_delivery) == {"changes", "commit", "verify"}


def test_reflection_exhaustion_is_not_a_final_success():
    from xenon.engine.reflection_engine import ReflectionEngine

    engine = ReflectionEngine(["fake/model"], max_rounds=1, pass_threshold=9)
    engine._execute = lambda *args, **kwargs: "incomplete"  # type: ignore[method-assign]
    engine._review = lambda *args, **kwargs: {  # type: ignore[method-assign]
        "score": 2,
        "pass": False,
        "feedback": "still incomplete",
        "issues": [],
    }

    result = engine.run_task("finish", context=AgentContext({}))

    assert "仍未通过审查" in result
    assert result.reason is TerminationReason.NO_PROGRESS
    assert result.ok is False
