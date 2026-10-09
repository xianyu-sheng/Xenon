"""单通道护栏：语义判断只允许出现在分类器 + 合并层（及降级回退）。

任何模块如果重新引入自己的 `detect_intent` 调用，都会在 CI 里失败。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_BANNED_MODULES = [
    "xenon/engine/react_engine.py",
    "xenon/engine/plan_execute_engine.py",
    "xenon/repl/auto_router.py",
]


def _source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_engines_and_router_never_call_detect_intent():
    for rel in _BANNED_MODULES:
        assert "detect_intent(" not in _source(rel), rel


def test_estimator_no_longer_falls_back_to_a_second_llm_call():
    source = _source("xenon/repl/difficulty_estimator.py")
    assert "classify_intent_with_llm" not in source


def test_repl_uses_regex_intent_only_in_the_degraded_fallback():
    source = _source("xenon/repl/turn_flow.py")
    # 唯一一处模块级 detect_intent 调用在 _detect_intent 帮助函数内部。
    assert source.count("return detect_intent(text)") == 1
    # 两处 self._detect_intent 调用都在 _resolve_turn_intent 的降级分支。
    assert source.count("self._detect_intent(") == 2
