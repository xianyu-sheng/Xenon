"""Intent-aware history projection (#165 follow-up): no unrelated contamination."""

from __future__ import annotations

from xenon.repl.context_manager import ContextManager


def _seed_project_task(cm: ContextManager) -> None:
    cm.add_user_message(
        "分析这个项目的代码，生成文档保留在当前文件夹",
        metadata={"intent": "write_doc"},
    )
    cm.add_tool_trace(
        "read_file",
        {"file_path": "xenon/utils/weather.py"},
        True,
        result="def get_weather(...): ..." * 50,
    )
    cm.add_assistant_message(
        "已生成 DEVELOPER_HANDOVER.md，包含安装、架构、天气工具 API 参数。"
        + "无关项目说明" * 300
    )


def test_standalone_report_gets_no_old_history() -> None:
    cm = ContextManager()
    _seed_project_task(cm)
    cm.add_user_message(
        "查询一下天气，生成天气文档保存下来",
        metadata={"intent": "write_report"},
    )

    assert (
        cm.get_prompt_messages(
            intent="write_report",
            current_user_input="查询一下天气，生成天气文档保存下来",
        )
        == []
    )


def test_report_followup_keeps_only_recent_conversation() -> None:
    cm = ContextManager()
    _seed_project_task(cm)
    cm.add_user_message("查询一下天气", metadata={"intent": "query"})
    cm.add_assistant_message("北京 16°C，体感 13°C。")
    current = "把刚才的天气生成天气报告保存下来"
    cm.add_user_message(current, metadata={"intent": "write_report"})

    messages = cm.get_prompt_messages(
        intent="write_report", current_user_input=current
    )
    text = "\n".join(str(m.get("content", "")) for m in messages)

    assert "北京 16°C" in text
    assert "DEVELOPER_HANDOVER" not in text
    assert "工具结果" not in text
    assert "安装、架构" not in text


def test_standalone_query_drops_old_project_history() -> None:
    cm = ContextManager()
    _seed_project_task(cm)
    current = "查询一下上海天气"
    cm.add_user_message(current, metadata={"intent": "query"})

    assert cm.get_prompt_messages(intent="query", current_user_input=current) == []


def test_code_task_keeps_full_history() -> None:
    cm = ContextManager()
    _seed_project_task(cm)
    current = "修复 weather.py 的解析错误"
    cm.add_user_message(current, metadata={"intent": "debug"})

    messages = cm.get_prompt_messages(
        intent="debug", current_user_input=current
    )
    text = "\n".join(str(m.get("content", "")) for m in messages)

    assert "DEVELOPER_HANDOVER" in text
    assert "工具结果" in text


def test_react_engine_history_uses_projection() -> None:
    from xenon.engine.context import AgentContext
    from xenon.engine.react_engine import ReActEngine
    from xenon.repl.turn_contract import build_turn_contract

    cm = ContextManager()
    _seed_project_task(cm)
    query = "查询一下天气，生成天气文档保存下来"
    cm.add_user_message(query, metadata={"intent": "write_report"})
    contract = build_turn_contract(query)
    ctx = AgentContext({"_turn_contract": contract})
    engine = ReActEngine(["fake/model"])
    engine._ctx_mgr = cm

    assert engine._history_messages(ctx, current_user_input=query) == []
