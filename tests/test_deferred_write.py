"""延迟写入 + 待批 Gate：“先给代码，我让你写你再写”的回归。

真实事故：用户第一轮明确说写入要等确认，契约却因为句子里出现“写入文件”
直接给了 WRITE，plan-execute 抢先落盘；随后“好写入到桌面”“请写入”又被
判成闲聊，模型陷入“请先授权”的文字循环。本文件锁住修复：

* 延迟/条件式写入只登记提案，本轮不授予写权限；
* 待批 Gate 接受确认语（好写入到桌面 / 请写入 / 授权写入…），用上一轮
  提案的级别直接续跑，不走意图分类器。
"""

from __future__ import annotations

import pytest

from xenon.repl.execution_policy import ExecutionLevel, ExecutionPolicy, extract_execution_signals
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL
from xenon.repl.turn_contract import (
    PendingAction,
    build_turn_contract,
    continuation_hint,
    should_consume_pending,
)

INCIDENT = (
    "你好，我现在想要来做一个多步任务，第一步你先完成一个快排算法，"
    "使用Python编写回复在对话区域，然后我要写入文件，你需要在回复结尾"
    "告诉我是否要写入文件啊啥的，我让你写你在写入到我的桌面"
)


class _Result:
    intent = "debug"
    confidence = 0.95
    operations = ("read", "write")
    chat_only = False
    reasoning = "test"


class _Classifier:
    enabled = True
    confidence_threshold = 0.7

    def __init__(self, result=None):
        self.result = result or _Result()

    def classify(self, text, **kwargs):
        return self.result


class _ChatClassifier(_Classifier):
    def __init__(self):
        super().__init__(
            type(
                "R",
                (),
                {
                    "intent": "chat",
                    "confidence": 0.95,
                    "operations": (),
                    "chat_only": True,
                    "reasoning": "",
                },
            )()
        )


def _make_repl() -> REPL:
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    return REPL(registry=registry, streaming=False)


def _approval_pending(**overrides) -> PendingAction:
    defaults = dict(
        engine="plan-execute",
        level=ExecutionLevel.WRITE,
        intent="debug",
        operations=frozenset({"read", "write"}),
        reason="上一轮用户要求确认后再写入",
        promise="（待确认的写盘提案）",
        kind="approval",
    )
    defaults.update(overrides)
    return PendingAction(**defaults)


# ── 信号层 ────────────────────────────────────────────────


def test_deferral_signals_on_the_incident_text():
    signals = extract_execution_signals(INCIDENT)
    assert signals.deferred_write is True
    assert signals.no_write is False


@pytest.mark.parametrize(
    "text",
    [
        "我让你写你再写",
        "等我确认后再写",
        "先别写文件",
        "先回复在对话区域，然后我要写入文件",
        "先给我看看方案，确认后再写",
    ],
)
def test_more_deferral_phrasings(text):
    assert extract_execution_signals(text).deferred_write is True


@pytest.mark.parametrize("text", ["请写入", "写入吧", "保存吧"])
def test_bare_write_imperatives(text):
    signals = extract_execution_signals(text)
    assert signals.write_imperative is True
    assert signals.deferred_write is False
    # 必须进入 write_patterns，才能被契约当成显式结构（而非闲聊）。
    assert "write_imperative" in signals.write_patterns


# ── 契约层 ────────────────────────────────────────────────


def test_incident_turn_defers_the_write():
    contract = build_turn_contract(INCIDENT, classifier=_Classifier())
    assert contract.deferred_write is True
    assert contract.level < ExecutionLevel.WRITE
    assert contract.proposed_level is ExecutionLevel.WRITE
    assert "write" in contract.operations
    assert contract.ask_required is False


def test_incident_defers_even_without_the_classifier():
    contract = build_turn_contract(INCIDENT)
    assert contract.deferred_write is True
    assert contract.level < ExecutionLevel.WRITE


def test_explicit_write_to_desktop_is_granted():
    # 分类器此时是闲聊（无操作）：只有正则的目标词表能救回写入意图。
    signals = extract_execution_signals("好写入到桌面")
    assert signals.explicit_write is True
    contract = build_turn_contract("好写入到桌面", classifier=_ChatClassifier())
    assert contract.level is ExecutionLevel.WRITE
    assert contract.requires_write
    assert contract.deferred_write is False


def test_bare_imperative_without_context_asks_once():
    contract = build_turn_contract("请写入", classifier=_ChatClassifier())
    assert contract.proposed_level is ExecutionLevel.WRITE
    assert contract.ask_required is True
    assert contract.level is ExecutionLevel.READ_ONLY


# ── 待批 Gate 绑定 ────────────────────────────────────────


@pytest.mark.parametrize(
    "text", ["请写入", "好写入到桌面", "授权写入到 C:/tmp/q.py", "继续"]
)
def test_approval_gate_accepts_confirmation_phrases(text):
    pending = _approval_pending()
    assert should_consume_pending(pending, text) is True
    contract = build_turn_contract(text, pending=pending)
    assert contract.continuation is True
    assert contract.level is ExecutionLevel.WRITE


def test_gate_does_not_hijack_unrelated_topics():
    pending = _approval_pending()
    assert should_consume_pending(pending, "今天天气怎么样") is False
    contract = build_turn_contract("今天天气怎么样", pending=pending)
    assert contract.continuation is False


def test_approval_hint_tells_the_model_to_act():
    hint = continuation_hint(_approval_pending())
    assert "已确认" in hint
    assert "不要重新询问授权" in hint


# ── REPL 接线 ─────────────────────────────────────────────


def test_deferred_turn_registers_an_approval_gate():
    repl = _make_repl()
    contract = build_turn_contract(INCIDENT)  # 测试里分类器关闭，走降级路径
    repl._update_pending_action(
        mode="plan-execute",
        contract=contract,
        policy=ExecutionPolicy(contract.level, "test"),
    )

    assert repl._pending_action is not None
    assert repl._pending_action.kind == "approval"
    assert repl._pending_action.level is ExecutionLevel.WRITE
    assert "write" in repl._pending_action.operations


def test_repl_confirmation_consumes_the_gate_and_runs_tools(monkeypatch):
    repl = _make_repl()
    repl._pending_action = _approval_pending()
    calls: dict = {}

    def fake_engine(spec, user_input, model_ids):
        calls["spec"] = spec
        calls["contract"] = repl.agent_context.get("_turn_contract")
        repl.ctx_mgr.add_assistant_message("已写入。")

    monkeypatch.setattr(repl, "_run_engine", fake_engine)
    monkeypatch.setattr(
        repl, "_run_direct", lambda *a, **k: calls.setdefault("direct", True)
    )

    repl._handle_chat("请写入")

    assert calls.get("spec") is not None
    assert calls["spec"].name == "plan-execute"
    assert calls["contract"].level is ExecutionLevel.WRITE
    assert "direct" not in calls
    # 动作完成且模型未再提出新承诺 → Gate 消费后清除
    assert repl._pending_action is None
