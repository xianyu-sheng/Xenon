from xenon.engine.strategy_guide import (
    STRATEGY_GUIDE,
    StrategyAdvice,
    TaskSignals,
    get_strategy_advice,
    infer_task_signals,
)


def test_static_guide_is_compact_and_generic():
    assert len(STRATEGY_GUIDE) <= 1000
    assert "调试 Bug" not in STRATEGY_GUIDE
    assert "通用原则" in STRATEGY_GUIDE


def test_debug_advice_contains_composable_tools():
    advice = get_strategy_advice("debug", {"read_file", "search_files", "edit_file", "command"})
    assert isinstance(advice, StrategyAdvice)
    assert advice.intent == "debug"
    assert "search_files" in advice.prompt
    assert "read_file" in advice.prompt
    assert "edit_file" in advice.prompt
    assert advice.tip.startswith("调试任务")
    assert advice.prompt.count("command") == 2


def test_advice_is_filtered_to_available_tools():
    advice = get_strategy_advice("debug", {"read_file", "command"})
    assert "read_file" in advice.prompt
    assert "search_files" not in advice.prompt
    assert "edit_file" not in advice.prompt
    assert "command" in advice.prompt


def test_non_tool_intent_returns_no_strategy():
    advice = get_strategy_advice("chat", {"read_file", "command"})
    assert advice.intent is None
    assert advice.prompt == ""
    assert advice.tip == ""


def test_unknown_intent_is_safe():
    advice = get_strategy_advice(None, {"read_file"})
    assert advice == StrategyAdvice(intent=None, prompt="", tip="")


def test_advice_is_deterministic():
    tools = {"command", "read_file", "search_files", "edit_file"}
    assert get_strategy_advice("debug", tools) == get_strategy_advice("debug", tools)


def test_task_signals_are_structural_not_domain_keywords():
    signals = infer_task_signals("跨文件修改 src/a.py 和 src/b.py，修复后运行全量测试")
    assert signals.multi_file is True
    assert signals.needs_verification is True
    assert signals.needs_recovery is False


def test_complex_task_gets_bounded_advice():
    advice = get_strategy_advice(
        "debug",
        {"command", "search_files", "read_file", "edit_file"},
        "跨文件修复 src/a.py 和 src/b.py，失败后重新验证",
    )
    assert isinstance(advice.signals, TaskSignals)
    assert advice.signals.multi_file is True
    assert "跨文件" in advice.prompt
    assert "不要机械调用" in advice.prompt


def test_simple_task_does_not_get_complexity_noise():
    advice = get_strategy_advice("debug", {"read_file", "edit_file"}, "修复 a.py")
    assert advice.signals.multi_file is False
    assert "复杂任务" not in advice.prompt
