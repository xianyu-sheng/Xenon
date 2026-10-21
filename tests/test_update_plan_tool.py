"""C11: persistent update_plan tool for ReAct long tasks."""

from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.engine.react_engine import ReActEngine
from xenon.nodes.tool_node import ToolNode


def _run(steps, **kwargs):
    context = AgentContext({})
    node = ToolNode("t", action_type="update_plan", steps=steps, **kwargs)
    result = node.execute(context)
    return result, context


def test_update_plan_stores_plan_and_renders_checklist() -> None:
    result, context = _run(
        [
            {"step": "read module", "status": "completed"},
            {"step": "change API", "status": "in_progress"},
            {"step": "run tests", "status": "pending"},
        ],
        explanation="API changed first",
    )

    assert result["success"] is True
    assert "- [x] read module" in result["content"]
    assert "- [>] change API" in result["content"]
    assert "- [ ] run tests" in result["content"]
    plan = context.get("_agent_plan")
    assert plan["steps"][1] == {"step": "change API", "status": "in_progress"}
    assert plan["explanation"] == "API changed first"


def test_update_plan_accepts_json_string_and_status_aliases() -> None:
    result, context = _run(
        '[{"step": "a", "status": "done"}, {"step": "b", "status": "in-progress"}]'
    )
    assert result["success"] is True
    assert context.get("_agent_plan")["steps"][0]["status"] == "completed"
    assert context.get("_agent_plan")["steps"][1]["status"] == "in_progress"


def test_update_plan_rejects_multiple_in_progress() -> None:
    result, context = _run(
        [
            {"step": "a", "status": "in_progress"},
            {"step": "b", "status": "in_progress"},
        ]
    )
    assert result["success"] is False
    assert "最多只能有一个" in result["error"]
    assert context.get("_agent_plan") is None


def test_update_plan_is_in_tools_and_native_schema() -> None:
    engine = ReActEngine(["fake/model"], native_fc=True)
    assert "update_plan" in engine.tools
    assert "update_plan" in engine.system_prompt
    assert "复杂任务维护计划" in engine.system_prompt
    schema = engine._build_tools_schema()
    item = next(x for x in schema if x["function"]["name"] == "update_plan")
    steps = item["function"]["parameters"]["properties"]["steps"]
    assert steps["type"] == "array"
    assert steps["items"]["properties"]["status"]["enum"] == [
        "pending",
        "in_progress",
        "completed",
    ]


def test_multifile_task_injects_plan_nudge(monkeypatch) -> None:
    """跨文件任务即使意图分类缺失，也要提示先 update_plan。"""
    engine = ReActEngine(["fake/model"], native_fc=False)
    seen: list[dict] = []

    class StopRun(Exception):
        pass

    def fake_llm(messages, **kwargs):
        seen.extend(messages)
        raise StopRun("captured first prompt")

    monkeypatch.setattr(engine, "_call_llm", fake_llm)
    try:
        engine.run(
            "Change shop/pricing.py, shop/cart.py and shop/invoice.py; "
            "update test_shop.py and run pytest.",
            AgentContext({}),
        )
    except StopRun:
        pass

    blob = chr(10).join(str(m.get("content", "")) for m in seen)
    assert "`update_plan`" in blob
    assert "跨文件/多步任务" in blob


def test_incomplete_plan_nudges_before_final(monkeypatch) -> None:
    engine = ReActEngine(["fake/model"], native_fc=False)
    ctx = AgentContext({})
    ctx.set("_agent_plan", {"steps": [{"step": "run tests", "status": "pending"}]})
    warnings: list[str] = []
    engine.callback.on_warning = warnings.append
    monkeypatch.setattr(
        engine,
        "_call_llm",
        lambda *a, **k: '{"thought": "done", "final_answer": "ok"}',
    )

    result = engine.run("do the task", ctx)

    assert result.startswith("ok")
    assert "计划未完成" in result
    assert any("计划仍有未完成步骤" in w for w in warnings)
    assert any("update_plan" in w for w in warnings)
    assert any("计划收尾提醒用尽" in w for w in warnings)


def test_missing_delivery_nudges_before_accepting_final(monkeypatch) -> None:
    """A write task cannot end silently without its required verification."""
    from xenon.engine.task_state import TaskState

    engine = ReActEngine(["fake/model"], native_fc=False)
    ctx = AgentContext({})
    state = TaskState(
        objective="implement #22",
        execution_level=3,
        required=["verify"],
    )
    ctx.set("_task_state", state.to_dict())
    warnings: list[str] = []
    engine.callback.on_warning = warnings.append
    monkeypatch.setattr(
        engine,
        "_call_llm",
        lambda *a, **k: '{"thought": "done", "final_answer": "ok"}',
    )

    result = engine.run_task("do the task", ctx)

    assert "任务交付未完成" in result
    assert any("引擎交付" in w for w in warnings)
    assert ctx.get("_task_state")["status"] == "active"


def test_completed_delivery_marks_task_complete_without_nudge(monkeypatch) -> None:
    from xenon.engine.task_state import TaskState

    engine = ReActEngine(["fake/model"], native_fc=False)
    ctx = AgentContext({})
    state = TaskState(
        objective="implement #22",
        execution_level=3,
        required=["verify", "commit"],
        artifacts=["src/feature.py"],
        committed_artifacts=["src/feature.py"],
        tests=["pytest -q"],
        commit="abc1234",
    )
    ctx.set("_task_state", state.to_dict())
    warnings: list[str] = []
    engine.callback.on_warning = warnings.append
    monkeypatch.setattr(
        engine, "_call_llm", lambda *a, **k: '{"thought": "done", "final_answer": "ok"}'
    )

    result = engine.run_task("do the task", ctx)

    assert result == "ok"
    assert not any("任务交付未完成" in w for w in warnings)
    assert ctx.get("_task_state")["status"] == "completed"
