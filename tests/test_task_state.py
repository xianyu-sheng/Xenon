"""任务状态块 + 分类器跨轮次字段（bind_gate_id / await_confirmation）。"""

from __future__ import annotations

from types import SimpleNamespace

from xenon.repl.execution_policy import ExecutionLevel
from xenon.repl.llm_intent_classifier import ClassificationResult, LLMIntentClassifier
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL
from xenon.repl.task_state import build_task_state_block
from xenon.repl.turn_contract import PendingAction, build_turn_contract


# ── 状态块 ─────────────────────────────────────────────────


def test_task_state_block_carries_goal_gate_artifacts_and_plan_mode():
    goal = SimpleNamespace(objective="修复快排脚本")
    pending = PendingAction(
        engine="plan-execute",
        level=ExecutionLevel.WRITE,
        intent="debug",
        operations=frozenset({"write"}),
        reason="等待确认写入桌面",
        promise="",
        kind="approval",
        gate_id="g1",
    )
    block = build_task_state_block(
        active_goal=goal,
        pending=pending,
        artifacts=["C:/Users/Administrator/Desktop/quicksort.py"],
        plan_mode=True,
    )
    assert "修复快排脚本" in block
    assert "id=g1" in block
    assert "等待确认写入桌面" in block
    assert "quicksort.py" in block
    assert "计划模式" in block


def test_task_state_block_is_budget_bounded_and_empty_when_nothing():
    long_goal = SimpleNamespace(objective="长目标" * 100)
    block = build_task_state_block(
        active_goal=long_goal,
        artifacts=["/a"] * 10,
        budget=80,
    )
    assert len(block) <= 120
    assert build_task_state_block() == ""


# ── 分类器解析跨轮次字段 ──────────────────────────────────


def test_parse_response_reads_continuation_fields():
    result = LLMIntentClassifier._parse_response(
        '{"intent":"debug","confidence":0.9,"operations":["write"],'
        '"continuation":{"bind_gate_id":"g1","await_confirmation":true}}'
    )
    assert result.bind_gate_id == "g1"
    assert result.await_confirmation is True


def test_parse_response_defaults_when_continuation_missing():
    result = LLMIntentClassifier._parse_response(
        '{"intent":"chat","confidence":0.9,"operations":[]}'
    )
    assert result.bind_gate_id == ""
    assert result.await_confirmation is False


# ── 合并层执行绑定 ────────────────────────────────────────


class _BindClassifier:
    enabled = True
    confidence_threshold = 0.7

    def __init__(self, *, bind="", await_confirm=False, ops=(), intent="debug"):
        self.result = ClassificationResult(
            intent=intent,
            confidence=0.9,
            operations=ops,
            bind_gate_id=bind,
            await_confirmation=await_confirm,
        )
        self.last_task_state: str | None = None

    def classify(self, text, *, context_messages=None, hints=None, task_state=""):
        self.last_task_state = task_state
        return self.result


def _pending(**overrides) -> PendingAction:
    defaults = dict(
        engine="plan-execute",
        level=ExecutionLevel.WRITE,
        intent="debug",
        operations=frozenset({"write"}),
        reason="等待确认写入桌面",
        promise="",
        kind="approval",
        gate_id="g1",
    )
    defaults.update(overrides)
    return PendingAction(**defaults)


def test_classifier_binds_the_open_gate():
    fake = _BindClassifier(bind="g1")
    # 这句话不在正则捷径词表里，只有分类器的 bind_gate_id 能接住。
    contract = build_turn_contract(
        "Windows系统，放桌面上", classifier=fake, pending=_pending(), task_state="[状态]"
    )
    assert contract.continuation is True
    assert contract.level is ExecutionLevel.WRITE


def test_classifier_await_confirmation_defers():
    fake = _BindClassifier(await_confirm=True, ops=("write",))
    # 这句不在正则延迟句式里，只有 await_confirmation 能触发延迟。
    contract = build_turn_contract("给我方案，做完发我确认后再落地", classifier=fake)
    assert contract.deferred_write is True
    assert contract.level < ExecutionLevel.WRITE
    assert "write" in contract.operations


def test_task_state_reaches_the_classifier():
    fake = _BindClassifier()
    build_turn_contract(
        "把这个函数写到桌面上", classifier=fake, task_state="[任务状态]"
    )
    assert fake.last_task_state == "[任务状态]"


# ── REPL 接线 ─────────────────────────────────────────────


def test_repl_task_state_block_includes_pending(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._pending_action = _pending()

    block = repl._task_state_block()

    assert "id=g1" in block
    assert "等待确认写入桌面" in block


def test_classifier_cache_key_includes_task_state(monkeypatch):
    """不同任务状态（门/目标不同）必须重新分类，不能用旧缓存。"""
    from xenon.repl.llm_intent_classifier import LLMIntentClassifier

    classifier = LLMIntentClassifier(enabled=True, model="test/model")
    calls: list[str] = []

    def fake_call(text, ctx, *, hints=None, task_state=""):
        calls.append(task_state)
        return ClassificationResult(intent="chat", confidence=0.9)

    monkeypatch.setattr(classifier, "_call_llm_classifier", fake_call)

    classifier.classify("可以", task_state="[状态A]")
    classifier.classify("可以", task_state="[状态A]")
    classifier.classify("可以", task_state="[状态B]")

    assert calls == ["[状态A]", "[状态B]"]
