"""Tests for the LLM-led classifier and the deterministic merge layer.

All classifier interactions use a FakeClassifier: no network, deterministic.
The regex-only degradation path must reproduce the previous behavior exactly
(covered by tests/test_execution_policy_golden.py).
"""

from __future__ import annotations

from xenon.repl.execution_policy import ExecutionLevel
from xenon.repl.llm_intent_classifier import (
    ClassificationResult,
    LLMIntentClassifier,
)
from xenon.repl.turn_contract import build_turn_contract


class FakeClassifier:
    enabled = True
    confidence_threshold = 0.7

    def __init__(
        self,
        intent: str | None = None,
        operations: tuple[str, ...] = (),
        confidence: float = 0.9,
        reasoning: str = "",
        chat_only: bool = False,
        exc: Exception | None = None,
    ) -> None:
        self.result = ClassificationResult(
            intent=intent,
            confidence=confidence,
            reasoning=reasoning,
            operations=operations,
            chat_only=chat_only,
        )
        self.exc = exc
        self.calls: list[str] = []
        self.last_hints = None

    def classify(self, text, *, context_messages=None, hints=None):
        self.calls.append(text)
        self.last_hints = hints
        if self.exc is not None:
            raise self.exc
        return self.result


# ── 降级路径：没有分类器时与旧正则行为一致 ──────────────────
def test_disabled_classifier_degrades_to_regex() -> None:
    contract = build_turn_contract("把结果写到 output.txt")

    assert contract.degraded is True
    assert contract.level is ExecutionLevel.WRITE


def test_fallback_intent_is_used_when_classifier_disabled() -> None:
    """REPL 传入的上下文意图（如“结果呢”继承 query）不能被降级路径丢弃。"""
    contract = build_turn_contract("结果呢", fallback_intent="query")

    assert contract.intent == "query"
    assert contract.level is ExecutionLevel.READ_ONLY


def test_classifier_failure_degrades_to_regex() -> None:
    fake = FakeClassifier(exc=RuntimeError("boom"))
    contract = build_turn_contract("把结果写到 output.txt", classifier=fake)

    assert contract.degraded is True
    assert contract.level is ExecutionLevel.WRITE


def test_classifier_empty_output_degrades_to_regex() -> None:
    fake = FakeClassifier(intent=None, operations=(), confidence=0.0)
    contract = build_turn_contract("读一下 output.txt", classifier=fake)

    assert contract.degraded is True
    assert contract.level is ExecutionLevel.READ_ONLY


# ── 规则 1：硬约束不调用 LLM，禁令不可被推翻 ────────────────
def test_hard_constraint_skips_llm_and_vetoes_write() -> None:
    fake = FakeClassifier(
        intent="debug", operations=("read", "write"), confidence=0.99
    )
    contract = build_turn_contract(
        "不要修改任何文件，只在对话里回答", classifier=fake
    )

    assert fake.calls == []  # 省一次调用
    assert contract.level is ExecutionLevel.ANSWER_ONLY
    assert contract.allows_write is False


def test_negation_vetoes_llm_write_but_keeps_read() -> None:
    fake = FakeClassifier(
        intent="debug", operations=("read", "write"), confidence=0.95
    )
    contract = build_turn_contract(
        "不要修改任何文件，看看 src/main.py 的报错", classifier=fake
    )

    assert "write" not in contract.operations
    assert contract.level is ExecutionLevel.READ_ONLY
    assert contract.to_execution_policy().explicit_no_write is True


# ── 规则 2：显式结构优先于 LLM 遗漏 ─────────────────────────
def test_explicit_structure_beats_llm_chat_only() -> None:
    fake = FakeClassifier(
        intent="chat", operations=(), confidence=0.9, chat_only=True
    )
    contract = build_turn_contract("把结果写到 output.txt", classifier=fake)

    assert contract.level is ExecutionLevel.WRITE
    assert "write" in contract.operations


def test_explicit_execute_structure_is_kept() -> None:
    fake = FakeClassifier(intent="debug", operations=(), confidence=0.9)
    contract = build_turn_contract("改完代码后运行测试", classifier=fake)

    assert contract.level is ExecutionLevel.EXECUTE


# ── 规则 3：纯生成/聊天不得发明文件操作 ─────────────────────
def test_write_code_without_file_target_stays_chat() -> None:
    fake = FakeClassifier(
        intent="write_code", operations=("create", "read"), confidence=0.9
    )
    contract = build_turn_contract("帮我写一个排序函数", classifier=fake)

    assert contract.level is ExecutionLevel.ANSWER_ONLY
    assert contract.operations == frozenset()


def test_explain_intent_cannot_invent_writes() -> None:
    fake = FakeClassifier(
        intent="explain", operations=("read", "write"), confidence=0.9
    )
    contract = build_turn_contract("解释这段代码的逻辑", classifier=fake)

    assert contract.level < ExecutionLevel.WRITE


def test_llm_chat_only_flag_cannot_invent_writes() -> None:
    """分类器自报 chat_only 时，即使 intent 是 debug 也不能开写权限。"""
    fake = FakeClassifier(
        intent="debug",
        operations=("read", "write"),
        confidence=0.95,
        chat_only=True,
    )
    contract = build_turn_contract("看看这个报错", classifier=fake)

    assert contract.level < ExecutionLevel.WRITE


# ── 规则 4：低置信 + 无证据 → 询问而不是静默授权 ────────────
def test_low_confidence_write_proposal_requires_user_consent() -> None:
    fake = FakeClassifier(
        intent="debug", operations=("read", "write"), confidence=0.5
    )
    contract = build_turn_contract("处理一下这个任务", classifier=fake)

    assert contract.ask_required is True
    assert contract.level is ExecutionLevel.READ_ONLY  # 未确认前不放权
    assert contract.proposed_level is ExecutionLevel.WRITE

    approved = contract.approve()
    assert approved.ask_required is False
    assert approved.level is ExecutionLevel.WRITE


def test_advisory_questions_cannot_invent_writes() -> None:
    """征询解释（问原因/思路/建议）不是施工：分类器给出 write 也要收回。"""
    fake = FakeClassifier(
        intent="debug", operations=("read", "write"), confidence=0.95
    )

    for text in (
        "修复这个 bug 的思路是什么",
        "处理一下这个问题，告诉我原因",
        "优化这段代码有什么建议",
    ):
        contract = build_turn_contract(text, classifier=fake)
        assert contract.level < ExecutionLevel.WRITE, text


def test_advisory_does_not_override_explicit_write_structure() -> None:
    """显式写入结构仍然优先于征询语气。"""
    fake = FakeClassifier(
        intent="write_code", operations=("create",), confidence=0.95
    )
    contract = build_turn_contract(
        "把结果写到 output.txt，顺便告诉我原因", classifier=fake
    )

    assert contract.level >= ExecutionLevel.WRITE


# ── 规则 5：query/research 的下限与提示加固 ─────────────────
def test_query_intent_gets_read_floor() -> None:
    fake = FakeClassifier(intent="query", operations=(), confidence=0.9)
    contract = build_turn_contract("今天苏州天气怎么样", classifier=fake)

    assert contract.level is ExecutionLevel.READ_ONLY


def test_regex_hints_are_forwarded_to_classifier() -> None:
    fake = FakeClassifier(
        intent="write_code", operations=("create",), confidence=0.95
    )
    build_turn_contract("把结果写到 output.txt", classifier=fake)

    assert fake.last_hints is not None
    assert fake.last_hints["write_snippets"]
    assert any("output.txt" in s for s in fake.last_hints["write_snippets"])


# ── REPL 接线：唯一分类入口 + 注入分类器 ─────────────────
def _make_repl():
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.repl import REPL

    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    return REPL(registry=registry, streaming=False)


def test_repl_resolves_contract_with_injected_classifier() -> None:
    repl = _make_repl()
    fake = FakeClassifier(
        intent="debug", operations=("read", "write"), confidence=0.9
    )
    repl._intent_classifier = fake
    repl._intent_classifier_checked = True

    contract, policy, _intent, _source = repl._resolve_turn_contract(
        "把结果写到 output.txt"
    )

    assert fake.calls == ["把结果写到 output.txt"]
    assert contract.level is ExecutionLevel.WRITE
    assert policy.level is ExecutionLevel.WRITE
    assert contract.degraded is False


def test_repl_falls_back_to_regex_without_classifier() -> None:
    repl = _make_repl()
    repl._intent_classifier = None
    repl._intent_classifier_checked = True

    contract, policy, _intent, _source = repl._resolve_turn_contract(
        "把结果写到 output.txt"
    )

    assert contract.degraded is True
    assert policy.level is ExecutionLevel.WRITE


def test_repl_fallback_keeps_contextual_query_intent() -> None:
    """分类器关闭时，REPL 解析的上下文意图必须传给契约降级路径。"""
    repl = _make_repl()
    repl._intent_classifier = None
    repl._intent_classifier_checked = True
    repl.ctx_mgr.add_request_message(
        "今天苏州天气怎么样",
        metadata={"intent": "query", "original_user_input": "今天苏州天气怎么样"},
    )

    contract, policy, intent, _source = repl._resolve_turn_contract("结果呢")

    assert intent == "query"
    assert contract.intent == "query"
    assert policy.level is ExecutionLevel.READ_ONLY


# ── 分类器输出解析：封闭词表 + 无效值丢弃 ───────────────────
def test_parse_response_keeps_only_valid_operations() -> None:
    result = LLMIntentClassifier._parse_response(
        '{"intent":"debug","operations":["read","WRITE","teleport"],'
        '"chat_only":false,"confidence":0.9,"reasoning":"x"}'
    )

    assert result.operations == ("read", "write")
    assert result.chat_only is False


def test_parse_response_ignores_non_list_operations() -> None:
    result = LLMIntentClassifier._parse_response(
        '{"intent":"chat","operations":"write","confidence":0.8,"reasoning":"x"}'
    )

    assert result.operations == ()


# ── provider-aware 选型：只看已配置 provider，优先便宜模型 ──
class _FakeProvider:
    def __init__(self, key: str, models: list[str]) -> None:
        self.key = key
        self.models = models


def test_select_configured_model_prefers_fast_pattern(monkeypatch) -> None:
    from xenon.repl import provider_registry

    monkeypatch.setattr(
        provider_registry,
        "get_configured_providers",
        lambda **kwargs: [
            _FakeProvider("deepseek", ["deepseek-v4-pro", "deepseek-v4-flash"])
        ],
    )

    assert (
        LLMIntentClassifier._select_configured_model()
        == "deepseek/deepseek-v4-flash"
    )


def test_select_configured_model_falls_back_to_first_model(monkeypatch) -> None:
    from xenon.repl import provider_registry

    monkeypatch.setattr(
        provider_registry,
        "get_configured_providers",
        lambda **kwargs: [_FakeProvider("openai", ["gpt-5"])],
    )

    assert LLMIntentClassifier._select_configured_model() == "openai/gpt-5"


def test_select_configured_model_without_providers(monkeypatch) -> None:
    from xenon.repl import provider_registry

    monkeypatch.setattr(
        provider_registry,
        "get_configured_providers",
        lambda **kwargs: [],
    )

    assert LLMIntentClassifier._select_configured_model() == ""
