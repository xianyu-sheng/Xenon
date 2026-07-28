"""Run Xenon against official SWE-bench instances.

This adapter does not grade anything.  It only gives an unchanged official
problem statement and repository worktree to Xenon, then writes the resulting
``git diff`` in the prediction format consumed by the official SWE-bench
harness.  The harness remains the sole authority for resolved/unresolved.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from datasets import load_dataset

from xenon.engine.callbacks import EngineCallback
from xenon.engine.combined_engines import (
    PlanReactEngine,
    PlanReflectionEngine,
    ReactReflectionEngine,
)
from xenon.engine.context import AgentContext
from xenon.engine.novel_engine import NovelEngine
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.react_engine import ReActEngine
from xenon.engine.reflection_engine import ReflectionEngine
from xenon.repl.model_registry import ModelConfig
from xenon.utils.llm_client import UsageTracker, chat_completion


ALL_ENGINES = (
    "direct", "react", "plan-execute", "reflection", "plan-react",
    "plan-reflection", "react-reflection", "novel",
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def _prompt(instance: dict[str, Any]) -> str:
    return f"""You are fixing an official SWE-bench task in the current repository.
Work directly in this working directory.  Inspect the code, implement the
minimal correct fix, and run focused tests when practical.  Actually edit the
working tree; do not only describe a patch.  Do not use a reference patch and
do not change tests unless required by the issue.  Leave all code changes in
the working tree for grading.

Official issue statement:
{instance['problem_statement']}
"""


def _engine(name: str, model: str, config: ModelConfig, max_steps: int):
    models = [model]
    configs = {model: config}
    callback = EngineCallback()
    common = dict(model_configs=configs, callback=callback)
    if name == "react":
        return ReActEngine(models, max_iterations=max_steps, native_fc=True,
                           project_root=str(Path.cwd()), **common)
    if name == "plan-execute":
        return PlanExecuteEngine(models, max_steps=max_steps,
                                 max_mini_react_rounds=1, **common)
    if name == "reflection":
        return ReflectionEngine(models, max_rounds=max_steps, **common)
    if name == "plan-react":
        return PlanReactEngine(models, max_steps=max_steps,
                               react_iterations=max_steps, **common)
    if name == "plan-reflection":
        return PlanReflectionEngine(models, max_steps=max_steps,
                                    review_rounds=max_steps, **common)
    if name == "react-reflection":
        return ReactReflectionEngine(models, react_iterations=max_steps,
                                     review_rounds=max_steps, **common)
    if name == "novel":
        return NovelEngine(models, max_iterations=max_steps, **common)
    return None


def run_one(instance: dict[str, Any], engine_name: str, root: Path, model: str,
            max_steps: int, request_timeout: float) -> dict[str, Any]:
    instance_id = instance["instance_id"]
    worktree = root / instance_id / engine_name
    worktree.parent.mkdir(parents=True, exist_ok=True)
    source = root / "_source" / instance_id
    if not source.exists():
        raise FileNotFoundError(f"missing prepared official worktree: {source}")
    if worktree.exists():
        shutil.rmtree(worktree)
    shutil.copytree(source, worktree)
    os.chdir(worktree)
    config = ModelConfig(model_id=model, alias=model, max_tokens=8192,
                         context_window=1_000_000)
    started = time.time()
    output = ""
    error = None
    trace: list[dict[str, Any]] = []
    usage_tracker = UsageTracker()
    try:
        if engine_name == "direct":
            output = chat_completion(model, [{"role": "user", "content": _prompt(instance)}],
                                     max_tokens=1024, temperature=0.3,
                                     timeout=request_timeout)
        else:
            engine = _engine(engine_name, model, config, max_steps)
            engine.request_timeout = request_timeout
            output = engine.run(_prompt(instance), AgentContext()) or ""
            tracker = getattr(engine, "_last_tracker", None)
            for call in list(getattr(tracker, "calls", []) or []):
                trace.append({
                    "tool": getattr(call, "tool_name", "unknown"),
                    "success": bool(getattr(call, "success", False)),
                    "error": getattr(call, "error", None),
                    "result": str(getattr(call, "result_summary", ""))[:500],
                })
    except Exception as exc:  # one engine must not abort the matrix
        error = f"{type(exc).__name__}: {exc}"
    finally:
        provider_usage = usage_tracker.snapshot()
        usage_tracker.close()
    patch = _git(worktree, "diff")
    prompt_tokens = sum(x["prompt_tokens"] for x in provider_usage.values())
    cache_hits = sum(x["cache_hit_tokens"] for x in provider_usage.values())
    cache_misses = sum(x["cache_miss_tokens"] for x in provider_usage.values())
    cache_denominator = cache_hits + cache_misses
    result = {
        "instance_id": instance_id,
        "model_name_or_path": f"xenon/{engine_name}/{model}",
        "model_patch": patch,
        "engine": engine_name,
        "elapsed_seconds": round(time.time() - started, 3),
        "error": error,
        "output_tail": output[-2000:],
        "tool_trace": trace,
        "provider_usage": provider_usage,
        "cache_metrics": {
            "cache_hit_rate": (
                cache_hits / cache_denominator if cache_denominator else None
            ),
            "reusable_tokens": cache_hits,
            "prompt_tokens": prompt_tokens,
            # Provider cache-hit tokens are evidence of reuse, not proof that
            # the same number of billed tokens disappeared.
            "estimated_tokens_saved": None,
            "estimated_cost_saved": None,
        },
        "context_metrics": {
            "compression_triggered": False,
            "tokens_before": None,
            "tokens_after": None,
        },
        "worktree": str(worktree),
    }
    (worktree / ".xenon_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="SWE-bench/SWE-bench_Lite")
    parser.add_argument("--split", default="test")
    parser.add_argument("--instance-id", action="append", required=True)
    parser.add_argument("--prepared-root", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--engines", nargs="+", default=["all"],
                        choices=["all", *ALL_ENGINES])
    parser.add_argument("--max-steps", type=int, default=3)
    parser.add_argument("--request-timeout", type=float, default=60)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--traces", type=Path, required=True)
    args = parser.parse_args()
    ids = set(args.instance_id)
    data = load_dataset(args.dataset, split=args.split)
    instances = [dict(row) for row in data if row["instance_id"] in ids]
    if len(instances) != len(ids):
        found = {x["instance_id"] for x in instances}
        raise SystemExit(f"unknown official instance(s): {sorted(ids - found)}")
    engines = list(ALL_ENGINES if "all" in args.engines else args.engines)
    results = []
    original = Path.cwd()
    try:
        try:
            for instance in instances:
                for name in engines:
                    os.chdir(original)
                    results.append(run_one(instance, name, args.prepared_root,
                                           args.model, args.max_steps,
                                           args.request_timeout))
                    # A provider outage or manual stop must not erase already
                    # completed engine runs.
                    args.traces.parent.mkdir(parents=True, exist_ok=True)
                    args.traces.with_suffix(".checkpoint.json").write_text(
                        json.dumps(results, ensure_ascii=False, indent=2)
                    )
        except KeyboardInterrupt:
            print("Interrupted; writing completed engine checkpoints.")
    finally:
        os.chdir(original)
    args.predictions.parent.mkdir(parents=True, exist_ok=True)
    prediction_files: dict[str, str] = {}
    # SWE-bench indexes predictions by instance_id, so different Xenon engines
    # must never share one file: duplicate IDs would silently overwrite each
    # other before grading.
    for engine_name in engines:
        path = args.predictions
        if len(engines) > 1:
            suffix = path.suffix or ".jsonl"
            path = path.with_name(f"{path.stem}.{engine_name}{suffix}")
        with path.open("w", encoding="utf-8") as f:
            for row in results:
                if row["engine"] != engine_name:
                    continue
                f.write(json.dumps({k: row[k] for k in
                                    ("instance_id", "model_name_or_path", "model_patch")},
                                   ensure_ascii=False) + "\n")
        prediction_files[engine_name] = str(path)
    args.traces.parent.mkdir(parents=True, exist_ok=True)
    args.traces.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps({"instances": len(instances), "engines": engines,
                      "predictions": prediction_files,
                      "traces": str(args.traces)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
