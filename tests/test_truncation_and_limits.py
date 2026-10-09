"""截断诚实标注 + 硬编码限额环境变量化（无魔法数字）。"""

from __future__ import annotations

import json

from xenon.engine.base import _default_max_tokens
from xenon.repl.repl import _retry_budget_for
from xenon.turn.gate import TurnGate
from xenon.utils.llm_client import _mark_truncated, _max_continuations


def test_mark_truncated_appends_to_final_answer():
    repaired = '{"final_answer": "分析了一半", "thought": "t"}'
    out = _mark_truncated(repaired)
    data = json.loads(out)  # 仍是合法 JSON
    assert "被截断" in data["final_answer"]
    assert data["thought"] == "t"


def test_mark_truncated_plain_text():
    out = _mark_truncated("普通文本被截")
    assert out.startswith("普通文本被截")
    assert "被截断" in out


def test_max_continuations_env(monkeypatch):
    monkeypatch.delenv("XENON_MAX_CONTINUATIONS", raising=False)
    assert _max_continuations() == 3
    monkeypatch.setenv("XENON_MAX_CONTINUATIONS", "7")
    assert _max_continuations() == 7
    monkeypatch.setenv("XENON_MAX_CONTINUATIONS", "garbage")
    assert _max_continuations() == 3


def test_default_max_tokens_env(monkeypatch):
    monkeypatch.delenv("XENON_MAX_TOKENS", raising=False)
    assert _default_max_tokens() == 8192
    monkeypatch.setenv("XENON_MAX_TOKENS", "32768")
    assert _default_max_tokens() == 32768
    monkeypatch.setenv("XENON_MAX_TOKENS", "garbage")
    assert _default_max_tokens() == 8192


def test_retry_budget_clamp_configurable(monkeypatch):
    monkeypatch.delenv("XENON_RETRY_BUDGET_MIN", raising=False)
    monkeypatch.delenv("XENON_RETRY_BUDGET_MAX", raising=False)
    assert _retry_budget_for(30) == 15
    monkeypatch.setenv("XENON_RETRY_BUDGET_MIN", "5")
    monkeypatch.setenv("XENON_RETRY_BUDGET_MAX", "60")
    assert _retry_budget_for(100) == 50
    assert _retry_budget_for(4) == 5  # 下限生效
    monkeypatch.setenv("XENON_RETRY_BUDGET_MIN", "garbage")
    assert _retry_budget_for(100) == 50  # 无效值回退 10


def test_gate_fuse_threshold_env(monkeypatch):
    monkeypatch.delenv("XENON_GATE_FUSE_THRESHOLD", raising=False)
    assert TurnGate().max_same_cause_failures == 2
    monkeypatch.setenv("XENON_GATE_FUSE_THRESHOLD", "5")
    assert TurnGate().max_same_cause_failures == 5
    monkeypatch.setenv("XENON_GATE_FUSE_THRESHOLD", "garbage")
    assert TurnGate().max_same_cause_failures == 2
    assert TurnGate(max_same_cause_failures=3).max_same_cause_failures == 3
