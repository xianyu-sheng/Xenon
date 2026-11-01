"""单通道护栏：语义判断只允许出现在分类器 + 合并层（及降级回退）。

任何模块如果重新引入自己的 `detect_intent` 调用，都会在 CI 里失败。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The engine modules that used to appear here are deleted; the guard now
# covers the modules that still make routing decisions.
_BANNED_MODULES = [
    "xenon/kernel/runtime.py",
    "xenon/harness/llm.py",
    "xenon/harness/runner.py",
    "xenon/repl/auto_router.py",
]


def _source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_loop_and_router_never_call_detect_intent():
    for rel in _BANNED_MODULES:
        assert "detect_intent(" not in _source(rel), rel


def test_estimator_no_longer_falls_back_to_a_second_llm_call():
    source = _source("xenon/repl/difficulty_estimator.py")
    assert "classify_intent_with_llm" not in source


def test_repl_does_not_use_regex_intent_for_routing():
    source = _source("xenon/repl/turn_flow.py")
    assert "detect_intent(" not in source
    # Follow-up inheritance reads stored turn metadata; it must not classify
    # the new utterance with keywords.
    assert "self._detect_intent(" not in source
