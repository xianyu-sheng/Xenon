"""Phase 2c tests: contract consumption, intent dedup, escalation ask flow."""

from __future__ import annotations

from types import SimpleNamespace

from xenon.engine.context import AgentContext
from xenon.engine.react_engine import ReActEngine
from xenon.nodes.tool_executor import ToolExecutor
from xenon.repl.difficulty_estimator import DifficultyEstimator
from xenon.repl.execution_policy import ExecutionLevel, classify_execution_policy
from xenon.repl.llm_intent_classifier import (
    ClassificationResult,
    LLMIntentClassifier,
)
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL
from xenon.repl.turn_contract import build_turn_contract


class _FakeClassifier:
    enabled = True
    confidence_threshold = 0.7

    def __init__(self, intent="debug", operations=("read", "write"), confidence=0.95):
        self.result = ClassificationResult(
            intent=intent,
            confidence=confidence,
            operations=tuple(operations),
        )

    def classify(self, text, *, context_messages=None, hints=None):
        return self.result


def _make_repl() -> REPL:
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    return REPL(registry=registry, streaming=False)


# ── 契约消费：引擎不再重算 ───────────────────────────────────
def test_react_reads_level_from_turn_contract() -> None:
    contract = build_turn_contract(
        "把结果写到 output.txt", classifier=_FakeClassifier()
    )
    ctx = AgentContext({"_turn_contract": contract})

    intent, level = ReActEngine._resolve_intent_and_level(
        ctx, "把结果写到 output.txt"
    )

    assert intent == "debug"  # 分类器意图
    assert level == int(ExecutionLevel.WRITE)
    assert level == int(contract.level)


def test_react_prefers_contract_level_over_reclassification() -> None:
    # 文本正则分类会得到 WRITE，但本轮契约是 READ_ONLY（例如用户中途约束），
    # 引擎必须以契约为准。
    contract = build_turn_contract("读一下 output.txt")
    ctx = AgentContext(
        {"_turn_contract": contract, "_execution_level": int(ExecutionLevel.READ_ONLY)}
    )

    _intent, level = ReActEngine._resolve_intent_and_level(ctx, "把结果写到 output.txt")

    assert level == int(ExecutionLevel.READ_ONLY)


def test_react_uses_contract_level_when_no_execution_level() -> None:
    """只有契约、没有 _execution_level 时也必须以契约级别为准。"""
    contract = build_turn_contract("读一下 output.txt")  # READ_ONLY
    ctx = AgentContext({"_turn_contract": contract})

    _intent, level = ReActEngine._resolve_intent_and_level(
        ctx, "把结果写到 output.txt"
    )

    assert level == int(ExecutionLevel.READ_ONLY)


def test_react_legacy_path_without_contract_still_classifies() -> None:
    ctx = AgentContext({})

    _intent, level = ReActEngine._resolve_intent_and_level(ctx, "把结果写到 output.txt")

    assert level == int(ExecutionLevel.WRITE)


# ── 意图去重：路由/难度估计复用契约意图 ─────────────────────
def test_estimator_uses_provided_intent(monkeypatch) -> None:
    estimator = DifficultyEstimator()
    detected: list[str] = []
    monkeypatch.setattr(
        estimator,
        "_detect_intent",
        lambda text: detected.append(text) or "chat",
    )

    profile = estimator.estimate("任意文本", intent="debug")

    assert detected == []  # 不再触发（可能联网的）意图检测
    assert profile.intent == "debug"

    estimator.estimate("任意文本")
    assert detected == ["任意文本"]  # 未提供时保持旧行为


def test_select_turn_mode_forwards_contract_intent(monkeypatch) -> None:
    repl = _make_repl()
    captured: dict[str, str | None] = {}

    def fake_estimate(user_input, context_messages=None, *, intent=None):
        captured["intent"] = intent
        return SimpleNamespace(recommended_engine="direct", engine_confidence=0.0)

    monkeypatch.setattr(repl.auto_router.estimator, "estimate", fake_estimate)
    policy = classify_execution_policy("读取 src/main.py")

    repl._select_turn_mode("读取 src/main.py", policy, "debug")

    assert captured["intent"] == "debug"


def test_classifier_cache_prevents_duplicate_calls(monkeypatch) -> None:
    classifier = LLMIntentClassifier(enabled=True, model="test/model")
    calls: list[str] = []

    def fake_call(text, ctx, *, hints=None):
        calls.append(text)
        return ClassificationResult(intent="chat", confidence=0.9)

    monkeypatch.setattr(classifier, "_call_llm_classifier", fake_call)

    first = classifier.classify("同一个问题")
    second = classifier.classify("同一个问题")

    assert first is second
    assert calls == ["同一个问题"]


def test_run_direct_prefers_stored_contract_over_reclassification(monkeypatch) -> None:
    """库入口未传 policy 时，_run_direct 必须优先读本轮契约。"""
    repl = _make_repl()
    contract = build_turn_contract(
        "把结果写到 output.txt", classifier=_FakeClassifier()
    )
    repl.agent_context.update({"_turn_contract": contract})
    routed: list[str] = []
    monkeypatch.setattr(
        repl, "_run_react_engine", lambda user_input, model_ids: routed.append(user_input)
    )

    # 文本本身是问候（正则会判 ANSWER_ONLY），但契约是 WRITE → 走 ReAct。
    repl._run_direct("你好", ["openai/test"])

    assert routed == ["你好"]


# ── 升级询问流：能力不足 → 问用户，而不是硬拒 ────────────────
def _patch_tool_execution(monkeypatch):
    executed: list[str] = []

    def fake_execute(self, context):
        executed.append(self.action_type)
        return {"success": True, "content": "ok"}

    monkeypatch.setattr("xenon.nodes.tool_executor.ToolNode.execute", fake_execute)
    return executed


def test_escalation_approval_bumps_level_and_executes(monkeypatch) -> None:
    executed = _patch_tool_execution(monkeypatch)
    ctx = AgentContext({"_execution_level": int(ExecutionLevel.READ_ONLY)})
    calls: list[tuple[str, int]] = []
    ctx.set_escalation_callback(
        lambda tool, level, reason: calls.append((tool, level)) or True
    )

    result = ToolExecutor().execute(
        "write_file",
        {"file_path": "x.py", "content": "pass"},
        ctx,
        tools={"write_file": {"name": "write_file"}},
    )

    assert result.success is True
    assert executed == ["write_file"]
    assert ctx.get("_execution_level") == int(ExecutionLevel.WRITE)
    assert calls == [("write_file", int(ExecutionLevel.WRITE))]


def test_escalation_denied_asks_once_per_turn(monkeypatch) -> None:
    _patch_tool_execution(monkeypatch)
    ctx = AgentContext({"_execution_level": int(ExecutionLevel.READ_ONLY)})
    asks: list[str] = []
    ctx.set_escalation_callback(lambda tool, level, reason: asks.append(tool) or False)
    executor = ToolExecutor()
    params = {"file_path": "x.py", "content": "pass"}
    tools = {"write_file": {"name": "write_file"}}

    first = executor.execute("write_file", params, ctx, tools=tools)
    second = executor.execute("write_file", params, ctx, tools=tools)

    assert first.success is False
    assert second.success is False
    assert asks == ["write_file"]  # 同一轮同一工具只问一次
    assert ctx.get("_execution_level") == int(ExecutionLevel.READ_ONLY)


def test_escalation_not_asked_when_level_sufficient(monkeypatch) -> None:
    executed = _patch_tool_execution(monkeypatch)
    ctx = AgentContext({"_execution_level": int(ExecutionLevel.WRITE)})
    asked: list[str] = []
    ctx.set_escalation_callback(lambda *args: asked.append("x") or True)

    result = ToolExecutor().execute(
        "write_file",
        {"file_path": "x.py", "content": "pass"},
        ctx,
        tools={"write_file": {"name": "write_file"}},
    )

    assert result.success is True
    assert executed == ["write_file"]
    assert asked == []


def test_escalation_callback_exception_is_denied() -> None:
    ctx = AgentContext({"_execution_level": int(ExecutionLevel.READ_ONLY)})

    def broken(*args):
        raise RuntimeError("boom")

    ctx.set_escalation_callback(broken)

    assert ctx.request_escalation("command", 3, "reason") is False
