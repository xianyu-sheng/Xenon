"""Cross-turn pending action state machine (“回复继续” → 真的继续).

Covers the failure where a promise made in one turn ("reply 继续 and I'll
keep going") was re-classified as fresh chat on the next turn, so the model
answered "I have no authorization" instead of executing.
"""

from __future__ import annotations

from xenon.repl.execution_policy import ExecutionLevel
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL
from xenon.repl.turn_contract import (
    PendingAction,
    build_turn_contract,
    continuation_hint,
    detect_pending_action,
    is_continuation_utterance,
)


class _ExplodingClassifier:
    """Any call means the continuation path wrongly invoked the LLM."""

    enabled = True
    confidence_threshold = 0.7

    def classify(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("continuation must not call the LLM classifier")


def _make_repl() -> REPL:
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    return REPL(registry=registry, streaming=False)


def _pending(**overrides) -> PendingAction:
    defaults = dict(
        engine="plan-react",
        level=ExecutionLevel.EXECUTE,
        intent="debug",
        operations=frozenset({"read", "write", "execute"}),
        reason="上一轮承诺在用户确认后继续执行",
        promise="回复「继续」，我将继续完成修复",
    )
    defaults.update(overrides)
    return PendingAction(**defaults)


# ── 短确认语识别 ────────────────────────────────────────────


def test_continuation_phrases_accept_short_affirmations():
    for text in ["继续", "好的，继续", "开始吧", "go on", "OK!", "确认", " 继续。"]:
        assert is_continuation_utterance(text), text


def test_continuation_phrases_reject_new_instructions():
    for text in ["继续优化性能", "继续，但先跑测试", "帮我看下别的", "你好"]:
        assert not is_continuation_utterance(text), text


# ── 承诺识别 ────────────────────────────────────────────────


def test_detect_pending_action_from_real_log_promise():
    promise = (
        "下一步：回复「继续」，我将继续落盘符号链接守卫并重跑"
        " tests/test_path_safety.py；如需处理源码级建议，请一并授权。"
    )
    pending = detect_pending_action(
        promise,
        engine="plan-react",
        level=ExecutionLevel.EXECUTE,
        intent="debug",
        operations={"read", "write", "execute"},
    )
    assert pending is not None
    assert pending.engine == "plan-react"
    assert pending.level is ExecutionLevel.EXECUTE
    assert pending.intent == "debug"
    assert "继续" in pending.promise


def test_detect_pending_action_ignores_negated_and_plain_endings():
    arguments = dict(
        engine="react",
        level=ExecutionLevel.WRITE,
        intent="debug",
        operations={"write"},
    )
    assert detect_pending_action("已全部完成，无需继续。", **arguments) is None
    assert detect_pending_action("以上是分析结论，仅供参考。", **arguments) is None
    assert detect_pending_action("", **arguments) is None


def test_detect_pending_action_matches_english_promises():
    pending = detect_pending_action(
        "Reply 'continue' and I will finish the migration.",
        engine="react",
        level=ExecutionLevel.WRITE,
        intent="debug",
        operations={"write"},
    )
    assert pending is not None


# ── 契约继承 ────────────────────────────────────────────────


def test_continuation_inherits_pending_contract_without_classifier():
    contract = build_turn_contract(
        "继续", classifier=_ExplodingClassifier(), pending=_pending()
    )
    assert contract.continuation
    assert contract.level is ExecutionLevel.EXECUTE
    assert contract.intent == "debug"
    assert {"write", "execute"} <= contract.operations
    assert not contract.ask_required
    assert "延续上一轮" in contract.reason


def test_continuation_never_exceeds_pending_level():
    contract = build_turn_contract(
        "好",
        pending=_pending(
            level=ExecutionLevel.READ_ONLY, operations=frozenset({"read"})
        ),
    )
    assert contract.level is ExecutionLevel.READ_ONLY


def test_continuation_without_pending_stays_answer_only():
    contract = build_turn_contract("继续")
    assert not contract.continuation
    assert contract.level is ExecutionLevel.ANSWER_ONLY


def test_continuation_hint_states_authorized_scope():
    hint = continuation_hint(_pending())
    assert "读写文件 + 命令执行" in hint
    assert "不要重新询问授权" in hint


# ── REPL 接线 ───────────────────────────────────────────────


def test_repl_continuation_forces_pending_engine(monkeypatch):
    repl = _make_repl()
    repl._pending_action = _pending()
    calls: dict = {}

    def fake_engine(spec, user_input, model_ids):
        calls["spec"] = spec
        repl.ctx_mgr.add_assistant_message("修复已完成。")

    monkeypatch.setattr(repl, "_run_engine", fake_engine)
    monkeypatch.setattr(
        repl, "_run_direct", lambda *a, **k: calls.setdefault("direct", True)
    )
    repl._handle_chat("继续")

    assert calls.get("spec") is not None
    assert calls["spec"].name == "plan-react"
    assert "direct" not in calls
    # 完成且模型未再提出新承诺 → 承诺消费后清除
    assert repl._pending_action is None


def test_repl_continuation_without_pending_clarifies(monkeypatch):
    repl = _make_repl()
    called: list[str] = []
    monkeypatch.setattr(repl, "_run_direct", lambda *a, **k: called.append("direct"))
    monkeypatch.setattr(repl, "_run_engine", lambda *a, **k: called.append("engine"))

    repl._handle_chat("继续")

    assert called == []


def test_repl_registers_new_promise_after_turn(monkeypatch):
    repl = _make_repl()
    repl._pending_action = _pending()

    def fake_engine(spec, user_input, model_ids):
        repl.ctx_mgr.add_assistant_message(
            "已完成本地修复。下一步：回复「继续」，我将继续跑安全测试。"
        )

    monkeypatch.setattr(repl, "_run_engine", fake_engine)
    monkeypatch.setattr(repl, "_run_direct", lambda *a, **k: None)
    repl._handle_chat("继续")

    assert repl._pending_action is not None
    assert repl._pending_action.engine == "plan-react"


def test_repl_new_topic_drops_stale_pending(monkeypatch):
    repl = _make_repl()
    repl._pending_action = _pending()
    monkeypatch.setattr(repl, "_run_direct", lambda *a, **k: None)
    monkeypatch.setattr(repl, "_run_engine", lambda *a, **k: None)

    repl._handle_chat("今天天气怎么样")

    assert repl._pending_action is None


def test_pending_derives_operations_from_policy_level(monkeypatch):
    """契约 operations 为空但本轮实际授权了执行时，承诺不能降级。"""
    repl = _make_repl()
    repl.ctx_mgr.add_assistant_message("下一步：回复「继续」，我将继续跑安全测试。")
    from xenon.repl.execution_policy import ExecutionPolicy
    from xenon.repl.turn_contract import contract_for_level

    repl._update_pending_action(
        mode="plan-react",
        contract=contract_for_level(ExecutionLevel.ANSWER_ONLY),
        policy=ExecutionPolicy(ExecutionLevel.EXECUTE, "测试授权"),
    )

    assert repl._pending_action is not None
    assert "execute" in repl._pending_action.operations
    assert repl._pending_action.level is ExecutionLevel.EXECUTE
