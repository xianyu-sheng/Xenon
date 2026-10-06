"""Intent-contract accuracy evaluation.

Runs a labeled corpus through (a) the regex-only degradation path and (b) the
LLM classifier + deterministic merge, then reports level accuracy, safety
violations, required-operation recall and latency.

Usage:
    python evals/intent_eval.py                    # regex + real LLM
    python evals/intent_eval.py --baseline regex   # offline only
    python evals/intent_eval.py --out evals/results/intent_eval.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xenon.repl.llm_intent_classifier import get_llm_classifier  # noqa: E402
from xenon.repl.turn_contract import build_turn_contract  # noqa: E402

CORPUS = Path(__file__).with_name("intent_corpus.jsonl")


def load_cases() -> list[dict]:
    cases: list[dict] = []
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            cases.append(json.loads(line))
    return cases


def evaluate(name: str, cases: list[dict], classifier) -> tuple[dict, list[dict]]:
    rows: list[dict] = []
    latencies: list[float] = []
    for case in cases:
        started = time.perf_counter()
        contract = build_turn_contract(case["text"], classifier=classifier)
        elapsed = (time.perf_counter() - started) * 1000
        if classifier is not None and not contract.degraded:
            latencies.append(elapsed)
        ops = set(contract.operations)
        level_ok = contract.level.name in case["levels"]
        safety_ok = not (ops & set(case.get("forbid", [])))
        recall_ok = set(case.get("require", [])) <= ops
        rows.append(
            {
                "text": case["text"],
                "level": contract.level.name,
                "expected_levels": case["levels"],
                "ops": sorted(ops),
                "level_ok": level_ok,
                "safety_ok": safety_ok,
                "recall_ok": recall_ok,
                "ask": contract.ask_required,
                "degraded": contract.degraded,
            }
        )

    total = len(rows)
    passed = sum(r["level_ok"] and r["safety_ok"] and r["recall_ok"] for r in rows)
    sorted_lat = sorted(latencies)
    p95 = (
        sorted_lat[min(len(sorted_lat) - 1, int(round(len(sorted_lat) * 0.95)) - 1)]
        if sorted_lat
        else None
    )
    summary = {
        "name": name,
        "cases": total,
        "pass": passed,
        "pass_rate": round(passed / total, 3) if total else 0.0,
        "level_accuracy": round(sum(r["level_ok"] for r in rows) / total, 3) if total else 0.0,
        "safety_violations": sum(not r["safety_ok"] for r in rows),
        "recall_misses": sum(not r["recall_ok"] for r in rows),
        "p50_ms": round(statistics.median(latencies), 1) if latencies else None,
        "p95_ms": round(p95, 1) if p95 is not None else None,
    }
    failures = [
        r for r in rows if not (r["level_ok"] and r["safety_ok"] and r["recall_ok"])
    ]
    return summary, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", choices=["regex", "llm", "both"], default="both")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    cases = load_cases()
    if args.limit:
        cases = cases[: args.limit]

    runs: dict[str, tuple[dict, list[dict]]] = {}
    if args.baseline in ("regex", "both"):
        runs["regex"] = evaluate("regex", cases, None)
    if args.baseline in ("llm", "both"):
        classifier = get_llm_classifier()
        if not classifier.enabled:
            print("LLM 分类器不可用（无已配置 provider），跳过 LLM 评测")
        else:
            runs["llm"] = evaluate(f"llm:{classifier.model}", cases, classifier)

    header = (
        f"{'model':34s} {'cases':>5s} {'pass':>5s} {'level':>6s} "
        f"{'safety':>6s} {'recall':>6s} {'p50ms':>7s} {'p95ms':>7s}"
    )
    print(header)
    print("-" * len(header))
    for summary, _failures in runs.values():
        p50 = summary["p50_ms"] if summary["p50_ms"] is not None else "-"
        p95v = summary["p95_ms"] if summary["p95_ms"] is not None else "-"
        print(
            f"{summary['name']:34s} {summary['cases']:5d} {summary['pass']:5d} "
            f"{summary['level_accuracy']:6.2f} {summary['safety_violations']:6d} "
            f"{summary['recall_misses']:6d} {p50:>7} {p95v:>7}"
        )

    for key, (_summary, failures) in runs.items():
        if failures:
            print(f"\n--- {key} 未通过 {len(failures)} 例 ---")
            for row in failures:
                print(
                    f"  [{row['level']:11s}] ops={row['ops']} ask={row['ask']}  {row['text']}"
                )

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    key: {"summary": summary, "failures": failures}
                    for key, (summary, failures) in runs.items()
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nreport -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
