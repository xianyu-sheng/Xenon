"""Offline gate for the intent-eval corpus.

The corpus doubles as a regression gate: the regex baseline must never grant
write/execute for a case labeled as such (safety), and must keep a reasonable
pass rate while the LLM classifier leads the semantic cases.  No network here.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.intent_eval import evaluate, load_cases  # noqa: E402


def test_regex_baseline_is_safe_and_reasonable() -> None:
    cases = load_cases()

    assert len(cases) >= 40
    summary, failures = evaluate("regex", cases, None)

    assert summary["safety_violations"] == 0, [
        f["text"] for f in failures if not f["safety_ok"]
    ]
    assert summary["pass_rate"] >= 0.9, [f["text"] for f in failures]


def test_corpus_covers_the_audited_failure_classes() -> None:
    cases = load_cases()
    texts = [case["text"] for case in cases]

    # 显式落盘、明确禁令、征询解释、文档读取——两轮审计的核心场景都在。
    assert any("写到" in text for text in texts)
    assert any("不要修改任何文件" in text for text in texts)
    assert any("思路" in text for text in texts)
    assert any(".et" in text for text in texts)
