"""Regressions for long realtime lists and contextual query follow-ups."""

from __future__ import annotations

import json

from xenon.engine.context import AgentContext
from xenon.engine.hollow_detector import HollowDetector
from xenon.engine.react_engine import BUILTIN_TOOLS, ReActEngine
from xenon.nodes.tool_node import (
    ToolNode,
    _infer_time_window,
    _prefilter_time_records,
)
from xenon.nodes.tool_executor import ToolExecutor
from xenon.repl.context_manager import ContextManager
from xenon.repl.execution_policy import ExecutionLevel, classify_execution_policy
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.permissions import PermissionGate
from xenon.repl.prompt_optimizer import (
    is_contextual_followup,
    optimize_prompt,
)
from xenon.repl.repl import REPL


def _schedule_text() -> str:
    records: list[str] = ["昆山-常州 单程（共96车次）"]
    for index in range(96):
        hour, minute = divmod(index * 15, 60)
        arrival_minutes = index * 15 + 45
        arrival_hour, arrival_minute = divmod(arrival_minutes % (24 * 60), 60)
        records.append(
            "\n".join([
                f"{hour:02d}:{minute:02d}",
                "昆山南",
                "0时45分",
                f"G{7000 + index}",
                f"{arrival_hour:02d}:{arrival_minute:02d}",
                "常州",
                "二等座54",
                "预订",
                "票务说明" * 150,
            ])
        )
    return "\n\n".join(records)


def test_chinese_evening_constraint_is_normalized():
    assert _infer_time_window("晚上六点之后") == ("18:00", None)
    assert _infer_time_window("18:30-23:10") == ("18:30", "23:10")


def test_long_schedule_is_filtered_before_prefix_truncation():
    source = _schedule_text()
    assert len(source) > 50_000

    filtered, metadata = _prefilter_time_records(
        source,
        start_time="18:00",
        end_time="23:59",
        max_chars=30_000,
    )

    assert metadata["prefilter_applied"] is True
    assert metadata["records_detected"] == 96
    assert metadata["records_matched"] == 24
    assert "18:00" in filtered
    assert "23:45" in filtered
    assert "17:45\n昆山南" not in filtered


def test_structured_mcp_json_is_filtered_before_truncation():
    source = json.dumps({
        "trains": [
            {"train_no": "G1", "departure_time": "17:55"},
            {"train_no": "G2", "departure_time": "18:05"},
            {"train_no": "G3", "departure_time": "20:30"},
        ]
    }, ensure_ascii=False)

    filtered, metadata = _prefilter_time_records(
        source,
        start_time="18:00",
        end_time=None,
        max_chars=12_000,
    )

    payload = json.loads(filtered)
    assert [item["train_no"] for item in payload["trains"]] == ["G2", "G3"]
    assert metadata["filter_type"] == "time_window_json"
    assert metadata["records_matched"] == 2


def test_12306_pipe_records_are_filtered_inside_json():
    def record(train: str, departure: str) -> str:
        fields = ["secret", "预订", "240000", train, "SHH", "NJH", "KSH", "CZH"]
        fields.extend([departure, "20:00", "01:00", "Y"])
        return "|".join(fields)

    source = json.dumps({
        "data": {"result": [record("G1", "17:40"), record("G2", "18:20")]}
    }, ensure_ascii=False)
    filtered, metadata = _prefilter_time_records(
        source,
        start_time="18:00",
        end_time=None,
        max_chars=12_000,
    )

    result = json.loads(filtered)["data"]["result"]
    assert len(result) == 1
    assert "|G2|" in result[0]
    assert metadata["records_detected"] == 2


def test_tool_node_inherits_time_constraint_from_query_context():
    node = ToolNode("fetch", action_type="web_fetch", url="https://example.com/list")
    context = AgentContext({
        "_query_constraint_source": "查周五昆山到常州车票，晚上六点之后",
    })

    filtered, metadata = node._prefilter_result_text(_schedule_text(), context)

    assert metadata["filter_start_time"] == "18:00"
    assert "18:00" in filtered
    assert "06:00\n昆山南" not in filtered


def test_executor_keeps_full_bounded_prefiltered_observation(monkeypatch):
    filtered = "18:00 G7001\n" + ("晚间车次详情\n" * 600)

    def fake_execute(self, _context):
        return {
            "success": True,
            "content": filtered,
            "prefilter_applied": True,
        }

    monkeypatch.setattr(ToolNode, "execute", fake_execute)
    result = ToolExecutor().execute(
        "web_fetch",
        {"url": "https://example.com/trains"},
        AgentContext({"_execution_level": int(ExecutionLevel.READ_ONLY)}),
        tools={"web_fetch": {"name": "web_fetch"}},
    )

    assert result.success is True
    assert result.observation == filtered
    assert len(result.observation) > 3000


def test_web_fetch_query_parameter_is_not_rewritten_as_file_search():
    params = ToolNode.normalize_params(
        {"url": "https://example.com", "query": "G7200"},
        action_type="web_fetch",
    )
    assert params["query"] == "G7200"
    assert "search_pattern" not in params


def test_retrieval_followups_are_recognized_without_becoming_debug_tasks():
    long_followup = "为什么被截断了呢？是输出太长了吗？那你能不能在截断之前用我给的条件筛选呢？"
    assert is_contextual_followup(long_followup) is True
    assert is_contextual_followup("结果呢") is True
    assert is_contextual_followup("为什么这段 Python 代码运行失败") is False

    optimized, _, changed = optimize_prompt("结果呢", intent="query")
    assert changed is True
    assert "## 查询需求" in optimized
    assert "## 调试要求" not in optimized


def test_followup_inherits_query_source_and_read_only_policy():
    repl = REPL.__new__(REPL)
    repl.ctx_mgr = ContextManager()
    original = "查一下周五从昆山到常州的车票，晚上六点之后，按时间排列"
    repl.ctx_mgr.add_user_message(
        original,
        metadata={
            "intent": "query",
            "original_user_input": original,
            "intent_source": original,
        },
    )
    repl.ctx_mgr.add_assistant_message("查询结果不完整")

    intent, source = repl._resolve_turn_intent("结果呢")
    policy = classify_execution_policy("结果呢", intent=intent)

    assert intent == "query"
    assert source == original
    assert policy.level is ExecutionLevel.READ_ONLY


def test_handle_chat_routes_result_followup_back_to_read_only_react(monkeypatch):
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    registry.assign_role("planner", ["test"])
    repl = REPL(registry=registry, streaming=False, optimize_prompts=True)
    original = "查一下周五从昆山到常州的车票，晚上六点之后，按时间排列"
    repl.ctx_mgr.add_user_message(
        original,
        metadata={"intent": "query", "intent_source": original},
    )
    repl.ctx_mgr.add_assistant_message("上次结果被截断")
    captured: list[str] = []
    monkeypatch.setattr(repl, "_inject_project_context", lambda: None)
    monkeypatch.setattr(repl, "_inject_memories", lambda _text: None)
    monkeypatch.setattr(repl, "_commit_memory_usage", lambda: None)
    monkeypatch.setattr(repl, "_maybe_suggest_memory", lambda _text: None)
    monkeypatch.setattr(
        repl,
        "_run_react_engine",
        lambda prompt, _models: captured.append(prompt),
    )

    repl._handle_chat("结果呢")

    assert captured and "延续上轮任务" in captured[0]
    assert original in captured[0]
    assert repl.agent_context.get("_execution_level") == int(ExecutionLevel.READ_ONLY)
    assert repl.ctx_mgr.history[-1].metadata["intent"] == "query"


def test_legacy_debug_labeled_followup_still_reaches_original_query():
    repl = REPL.__new__(REPL)
    repl.ctx_mgr = ContextManager()
    original = "查一下周五从昆山到常州的车票，晚上六点之后"
    repl.ctx_mgr.add_user_message(original)
    repl.ctx_mgr.add_assistant_message("数据被截断")
    repl.ctx_mgr.add_user_message(
        "## 问题描述\n为什么被截断了？能不能先筛选\n## 调试要求\n给出代码"
    )
    repl.ctx_mgr.add_assistant_message("https://example.com/search")

    intent, source = repl._resolve_turn_intent("结果呢")

    assert intent == "query"
    assert source is not None and "昆山" in source


def test_query_url_only_and_placeholder_answers_are_hollow():
    detector = HollowDetector()
    url_only = detector.detect(
        "https://example.com/search?from=昆山&to=常州",
        require_query_result=True,
    )
    placeholder = detector.detect(
        "https://example.com/query?from={昆山电报码}&to={常州电报码}",
        require_query_result=True,
    )
    actual = detector.detect(
        "查询结果：G7201，18:35 从昆山南出发，19:18 到达常州。详情见 https://example.com",
        require_query_result=True,
    )

    assert url_only.is_hollow is True
    assert placeholder.is_hollow is True
    assert actual.is_hollow is False
    assert "链接或接口地址不是查询结果" in url_only.hint()


def test_mcp_confirmation_never_displays_unknown_server_marker():
    auto = PermissionGate.format_confirm_message(
        "mcp_call", {"tool_name": "train_query"}, "CRITICAL"
    )
    qualified = PermissionGate.format_confirm_message(
        "mcp_call", {"tool_name": "rail:train_query"}, "CRITICAL"
    )

    assert "自动路由 / train_query" in auto
    assert "? / train_query" not in auto
    assert "rail / train_query" in qualified


def test_mcp_tool_is_hidden_when_no_server_is_configured(monkeypatch):
    repl = REPL.__new__(REPL)
    engine = ReActEngine(["test/model"], tools=dict(BUILTIN_TOOLS), native_fc=False)
    monkeypatch.setattr(repl, "_build_mcp_tools_list", lambda: "")

    repl._inject_mcp_tools_into_engine(engine)

    assert "mcp_call" not in engine.tools
    assert "mcp_call" not in {
        item["function"]["name"] for item in engine._build_tools_schema()
    }
