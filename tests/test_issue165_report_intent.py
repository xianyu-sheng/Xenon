"""#165: weather + document must be a data report, not a technical doc."""

from __future__ import annotations

from xenon.engine.react_prompts import BUILTIN_TOOLS
from xenon.engine.strategy_guide import get_strategy_advice
from xenon.repl.llm_intent_classifier import (
    INTENT_CATEGORIES,
    ClassificationResult,
    LLMIntentClassifier,
)
from xenon.repl.prompt_optimizer import (
    detect_intent,
    get_intent_display,
    optimize_prompt,
)
from xenon.repl.turn_contract import build_turn_contract


class _WriteDocClassifier:
    enabled = True
    confidence_threshold = 0.7

    def classify(self, text, *, context_messages=None, hints=None, task_state=None):
        return ClassificationResult(
            intent="write_doc",
            confidence=0.95,
            operations=("create",),
            reasoning="fake generic doc classifier",
        )


def test_weather_document_is_report_not_technical_doc() -> None:
    query = "查询一下天气，生成天气文档保存下来"

    assert detect_intent(query) == "write_report"
    contract = build_turn_contract(query)
    assert contract.intent == "write_report"
    assert contract.level.name == "WRITE"
    assert {"read", "write"} <= set(contract.operations)


def test_report_intent_beats_a_generic_write_doc_classifier() -> None:
    contract = build_turn_contract(
        "查询一下天气，生成天气文档保存下来", classifier=_WriteDocClassifier()
    )

    assert contract.intent == "write_report"
    assert contract.level.name == "WRITE"


def test_report_template_has_no_install_or_api_sections() -> None:
    prompt, hint, changed = optimize_prompt(
        "查询一下天气，生成天气文档保存下来", intent="write_report"
    )

    assert changed is True
    assert "来源与时间" in prompt
    assert "安装/前置条件" not in prompt
    assert "用法：参数、示例、返回值" not in prompt
    assert "不是项目或 API 技术文档" in prompt
    assert "只写：报告路径" in prompt
    assert hint and "数据报告" in hint


def test_report_strategy_does_not_explore_project_code() -> None:
    advice = get_strategy_advice(
        "write_report",
        set(BUILTIN_TOOLS),
        "查询一下天气，生成天气文档保存下来",
    )

    assert "weather" in advice.prompt
    assert "不要探索仓库代码" in advice.prompt
    assert "只汇报报告路径" in advice.prompt
    assert "list_files" not in advice.prompt
    assert "search_files" not in advice.prompt


def test_project_document_stays_technical_doc() -> None:
    query = "分析这个项目的代码，生成文档保留在当前文件夹"

    assert detect_intent(query) == "write_doc"
    assert build_turn_contract(query).intent == "write_doc"


def test_report_intent_is_exposed_to_the_classifier() -> None:
    assert "write_report" in INTENT_CATEGORIES
    assert "write_report" in LLMIntentClassifier._build_system_prompt()
    assert "报告" in get_intent_display("write_report")
