"""Run Xenon against official SWE-bench instances.

This adapter does not grade anything.  It only gives an unchanged official
problem statement and repository worktree to Xenon, then writes the resulting
``git diff`` in the prediction format consumed by the official SWE-bench
harness.  The harness remains the sole authority for resolved/unresolved.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import queue as queue_module
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# 允许从任意 cwd 直接运行（python evals/swebench_xenon.py ...）：
# Python 只把脚本所在目录（evals/）放进 sys.path，仓库根目录不在其中，
# 跨包导入 evals.swebench_runtime 会 ModuleNotFoundError。显式补上根目录。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from datasets import load_dataset  # noqa: E402

from xenon.engine.callbacks import EngineCallback  # noqa: E402
from xenon.engine.combined_engines import (  # noqa: E402
    PlanReactEngine,
    PlanReflectionEngine,
    ReactReflectionEngine,
)
from xenon.engine.context import AgentContext  # noqa: E402
from xenon.engine.coding_contract import finalize_coding_run  # noqa: E402
from xenon.engine.execution_policy import (  # noqa: E402
    ExecutionPolicy,
    bind_execution_policy,
)
from xenon.engine.plan_execute_engine import PlanExecuteEngine  # noqa: E402
from xenon.engine.react_engine import ReActEngine  # noqa: E402
from xenon.engine.reflection_engine import ReflectionEngine  # noqa: E402
from xenon.engine.tool_runtime import bind_tool_runtime  # noqa: E402
from xenon.repl.model_registry import ModelConfig  # noqa: E402
from xenon.utils.llm_client import UsageTracker, chat_completion  # noqa: E402

from evals.swebench_runtime import (  # noqa: E402
    create_official_runtime,
    prepare_official_source,
)


def _all_engines() -> tuple[str, ...]:
    """从 ENGINE_REGISTRY 派生（单一真相源，不再硬编码）。"""
    # 惰性导入：避免 evals 模块 import 时触发引擎注册表加载
    import xenon.engine.builtin_engines  # noqa: F401  # 触发内置范式注册
    from xenon.engine.registry import ENGINE_REGISTRY
    return ENGINE_REGISTRY.names()

ALL_ENGINES = _all_engines()

# SWE-bench 是代码编辑评测：只有具备文件修改能力的引擎才有意义。
# direct（裸 LLM 单轮调用）与 reflection（纯文本 Generate-Critique-Refine）
# 不操作文件系统，永远产生空补丁——承认边界，不作为 `all` 的默认成员。
# 注：条件用 EngineSpec 的 runs_engine 还不够——reflection 也 runs_engine，
# 但它的修改循环是纯文本的，不操作文件系统。
_NON_CODE_EDITING: frozenset[str] = frozenset({"direct", "reflection"})

CODE_EDITING_ENGINES = tuple(
    n for n in ALL_ENGINES if n not in _NON_CODE_EDITING
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def _prompt(instance: dict[str, Any]) -> str:
    # FAIL_TO_PASS 测试名（test_patch 已应用，在原始代码上应失败）
    fail_tests = instance.get("FAIL_TO_PASS", [])
    fail_hint = ""
    if fail_tests:
        # 取前 3 个测试名（prompt 太长会稀释注意力）
        samples = fail_tests[:3]
        fail_hint = (
            "\n\n以下测试在原始代码上失败，你的修复应让它们通过：\n"
            + "\n".join(f"  - {t}" for t in samples)
            + ("\n  ...（等" if len(fail_tests) > 3 else "")
        )
    return f"""You are fixing an official SWE-bench task in the current repository.
Work directly in this working directory.  Inspect the code, implement the
minimal correct fix, and __run focused tests to verify your fix__.
Actually edit the working tree using the available tools; do not only describe
a patch.  Do not use a reference patch and do not change tests unless required
by the issue.  Leave all code changes in the working tree for grading.
If this engine cannot edit files with tools, return only a complete git-style
unified diff that can be applied with `git apply`; do not return a prose
approximation of a patch.{fail_hint}

Official issue statement:
{instance['problem_statement']}
"""


def _engine(name: str, model: str, config: ModelConfig, max_steps: int,
            verification_loop: bool = True):
    models = [model]
    configs = {model: config}
    callback = EngineCallback()
    common = dict(model_configs=configs, callback=callback)
    # 引擎构造仍用 if/elif 链——各引擎有独特 kwargs（native_fc、project_root、
    # max_mini_react_rounds 等），不能直接调 EngineSpec.factory() 透传。
    # 未来可在 EngineSpec 增加 swbench_kwargs 结构化元数据，届时收敛为查表。
    if name == "react":
        engine = ReActEngine(models, max_iterations=max_steps, native_fc=True,
                           project_root=str(Path.cwd()), **common)
    elif name == "plan-execute":
        engine = PlanExecuteEngine(models, max_steps=max_steps,
                                 max_mini_react_rounds=1, **common)
    elif name == "reflection":
        engine = ReflectionEngine(models, max_rounds=max_steps, **common)
    elif name == "plan-react":
        engine = PlanReactEngine(models, max_steps=max_steps,
                               react_iterations=max_steps, **common)
    elif name == "plan-reflection":
        engine = PlanReflectionEngine(models, max_steps=max_steps,
                                    review_rounds=max_steps, **common)
    elif name == "react-reflection":
        engine = ReactReflectionEngine(models, react_iterations=max_steps,
                                     review_rounds=max_steps, **common)
    else:
        return None
    # v0.8.3 A/B: 开关验证循环（引擎维度的全局开关）
    engine._verification_enabled = verification_loop
    return engine


def _append_event(path: Path | None, event: str, **fields: Any) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": time.time(), "event": event, **fields}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def run_one(instance: dict[str, Any], engine_name: str, root: Path, model: str,
            max_steps: int, request_timeout: float, engine_timeout: float,
            min_request_interval: float = 0.0,
            provider_attempts: int = 1,
            events_path: Path | None = None,
            namespace: str | None = "swebench",
            verification_loop: bool = True) -> dict[str, Any]:
    instance_id = instance["instance_id"]
    config = ModelConfig(model_id=model, alias=model, max_tokens=8192,
                         context_window=1_000_000)
    started = time.time()
    output = ""
    error = None
    trace: list[dict[str, Any]] = []
    usage_tracker = UsageTracker()
    worktree = root / instance_id / engine_name
    runtime_metadata: dict[str, Any] = {}
    coding_result = None
    policy = ExecutionPolicy.from_timeout(
        engine_timeout,
        request_timeout=request_timeout,
        provider_attempts=provider_attempts,
        chain_retries=0,
        min_request_interval=min_request_interval,
        event_sink=lambda event, fields: _append_event(
            events_path, event, instance_id=instance_id,
            engine=engine_name, **fields,
        ),
    )
    _append_event(events_path, "engine_start", instance_id=instance_id,
                  engine=engine_name)
    try:
        with create_official_runtime(
            instance, root, engine_name, namespace=namespace
        ) as runtime:
            worktree = runtime.host_worktree
            os.chdir(worktree)
            runtime_metadata = {
                "image_key": runtime.image_key,
                "image_id": runtime.image_id,
                "container_name": runtime.container_name,
                "container_workdir": runtime.container_workdir,
            }
            _append_event(events_path, "container_ready", instance_id=instance_id,
                          engine=engine_name, **runtime_metadata)
            if engine_name == "direct":
                policy.wait_for_request_slot("direct")
                policy.emit("provider_request_start", phase="direct")
                output = chat_completion(
                    model,
                    [{"role": "user", "content": _prompt(instance)}],
                    max_tokens=8192,
                    temperature=0.3,
                    timeout=policy.request_budget("direct"),
                    max_retries=policy.provider_attempts,
                )
                policy.emit("provider_request_end", phase="direct", success=True)
            else:
                engine = _engine(engine_name, model, config, max_steps,
                                 verification_loop=verification_loop)
                bind_execution_policy(engine, policy)
                bind_tool_runtime(engine, runtime.tool_runtime)
                output = engine.run(_prompt(instance), AgentContext()) or ""
                tracker = getattr(engine, "_last_tracker", None)
                for call in list(getattr(tracker, "calls", []) or []):
                    trace.append({
                        "tool": getattr(call, "tool_name", "unknown"),
                        "success": bool(getattr(call, "success", False)),
                        "state": getattr(call, "state", None),
                        "attempts": getattr(call, "attempts", None),
                        "elapsed_seconds": getattr(call, "elapsed_seconds", None),
                        "error": getattr(call, "error", None),
                        "result": str(getattr(call, "result_summary", ""))[:500],
                    })
            coding_result = finalize_coding_run(
                worktree, output, tool_trace=trace
            )
            _append_event(
                events_path,
                "patch_extracted",
                instance_id=instance_id,
                engine=engine_name,
                patch_source=coding_result.patch_source,
                apply_success=coding_result.apply_success,
                patch_bytes=len(coding_result.patch.encode()),
            )
    except Exception as exc:  # one engine must not abort the matrix
        error = f"{type(exc).__name__}: {exc}"
    finally:
        provider_usage = usage_tracker.snapshot()
        usage_tracker.close()
    patch = coding_result.patch if coding_result is not None else (
        _git(worktree, "diff") if worktree.exists() else ""
    )
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
        "patch_source": (
            coding_result.patch_source if coding_result is not None else "empty"
        ),
        "patch_apply_success": bool(
            coding_result is not None and coding_result.apply_success
        ),
        "patch_apply_error": (
            coding_result.apply_error if coding_result is not None else None
        ),
        "runtime": runtime_metadata,
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
    if worktree.exists():
        (worktree / ".xenon_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
    _append_event(events_path, "engine_end", instance_id=instance_id,
                  engine=engine_name, error=error,
                  elapsed_seconds=result["elapsed_seconds"])
    return result


def _child_run(queue: Any, kwargs: dict[str, Any]) -> None:
    try:
        queue.put({"result": run_one(**kwargs)})
    except BaseException as exc:  # parent must receive child bootstrap failures
        queue.put({"error": f"{type(exc).__name__}: {exc}"})


def _child_prepare(queue: Any, instance: dict[str, Any], root: Path,
                   namespace: str | None) -> None:
    try:
        source, image_key, image_id = prepare_official_source(
            instance, root, namespace=namespace
        )
        queue.put({
            "source": str(source), "image_key": image_key, "image_id": image_id,
        })
    except BaseException as exc:
        queue.put({"error": f"{type(exc).__name__}: {exc}"})


def _cleanup_engine_containers(instance_id: str, engine_name: str) -> None:
    """Remove only containers owned by one interrupted Xenon engine run."""

    try:
        import docker

        client = docker.from_env()
        prefix = f"sweb.xenon.{instance_id.lower()}.{engine_name}."
        for container in client.containers.list(all=True):
            if container.name.startswith(prefix):
                container.remove(force=True)
        client.close()
    except Exception:
        # The hard-timeout result is still authoritative.  A later invocation
        # can clean a daemon that was temporarily unavailable.
        pass


def _prepare_with_timeout(instance: dict[str, Any], root: Path,
                          namespace: str | None, timeout: float,
                          events_path: Path) -> dict[str, str]:
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(
        target=_child_prepare, args=(queue, instance, root, namespace)
    )
    process.start()
    started = time.monotonic()
    deadline = started + timeout
    _append_event(events_path, "image_prepare_start",
                  instance_id=instance["instance_id"], namespace=namespace)
    result: dict[str, Any] | None = None
    while result is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.terminate()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()
            _append_event(events_path, "image_prepare_timeout",
                          instance_id=instance["instance_id"], timeout=timeout)
            raise TimeoutError(
                f"official image preparation exceeded {timeout}s for "
                f"{instance['instance_id']}"
            )
        try:
            # Drain the pipe while the child is alive.  Waiting for child exit
            # before Queue.get can deadlock when the feeder pipe fills.
            result = queue.get(timeout=min(10.0, remaining))
        except queue_module.Empty:
            if not process.is_alive():
                raise RuntimeError(
                    "image preparation exited without a result "
                    f"({process.exitcode})"
                )
            _append_event(
                events_path, "image_prepare_heartbeat",
                instance_id=instance["instance_id"],
                elapsed_seconds=round(time.monotonic() - started, 3),
                remaining_seconds=round(remaining, 3),
            )
    process.join(5)
    if process.is_alive():
        process.terminate()
        process.join()
    if "error" in result:
        raise RuntimeError(result["error"])
    _append_event(events_path, "image_prepare_end",
                  instance_id=instance["instance_id"],
                  image_key=result["image_key"], image_id=result["image_id"])
    return result


def _run_with_hard_timeout(kwargs: dict[str, Any], timeout: float,
                           events_path: Path) -> dict[str, Any]:
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(target=_child_run, args=(queue, kwargs))
    process.start()
    deadline = time.monotonic() + timeout
    message: dict[str, Any] | None = None
    while message is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.terminate()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()
            _cleanup_engine_containers(
                kwargs["instance"]["instance_id"], kwargs["engine_name"]
            )
            _append_event(events_path, "hard_timeout",
                          instance_id=kwargs["instance"]["instance_id"],
                          engine=kwargs["engine_name"], timeout=timeout)
            return {
                "instance_id": kwargs["instance"]["instance_id"],
                "model_name_or_path": f"xenon/{kwargs['engine_name']}/{kwargs['model']}",
                "model_patch": "",
                "engine": kwargs["engine_name"],
                "elapsed_seconds": timeout,
                "error": f"EngineDeadlineExceeded: hard wall timeout ({timeout}s)",
                "output_tail": "",
                "tool_trace": [],
                "patch_source": "empty",
                "patch_apply_success": False,
                "patch_apply_error": "hard wall timeout",
                "runtime": {},
                "provider_usage": {},
                "cache_metrics": {},
                "context_metrics": {},
                "worktree": str(kwargs["root"] / kwargs["instance"]["instance_id"] / kwargs["engine_name"]),
            }
        try:
            # Read before join.  A completed engine result can exceed the OS
            # pipe buffer (tool traces + patch + telemetry); the child feeder
            # cannot exit until the parent drains it.
            message = queue.get(timeout=min(10.0, remaining))
        except queue_module.Empty:
            if not process.is_alive():
                raise RuntimeError(
                    f"engine child exited without a result ({process.exitcode})"
                )
            _append_event(events_path, "heartbeat",
                          instance_id=kwargs["instance"]["instance_id"],
                          engine=kwargs["engine_name"],
                          remaining_seconds=round(remaining, 3))
    process.join(5)
    if process.is_alive():
        process.terminate()
        process.join()
    if "error" in message:
        raise RuntimeError(message["error"])
    return message["result"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="SWE-bench/SWE-bench_Lite")
    parser.add_argument("--split", default="test")
    parser.add_argument("--instance-id", action="append", required=True)
    parser.add_argument("--prepared-root", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--engines", nargs="+", default=["all"],
                        choices=["all", *ALL_ENGINES])
    parser.add_argument(
        "--include-non-editing", action="store_true",
        help="同时运行 direct/reflection 等不修改文件系统的引擎（默认排除）。",
    )
    parser.add_argument("--max-steps", type=int, default=3)
    parser.add_argument("--request-timeout", type=float, default=60)
    parser.add_argument("--engine-timeout", type=float, default=900)
    parser.add_argument("--prepare-timeout", type=float, default=1800)
    parser.add_argument(
        "--min-request-interval", type=float, default=0.0,
        help="Minimum seconds between provider request starts in one engine graph.",
    )
    parser.add_argument(
        "--provider-attempts", type=int, default=1,
        help="Total attempts owned by the provider client for transient failures.",
    )
    parser.add_argument(
        "--namespace", default="swebench",
        help="Official image namespace; use 'none' to build locally",
    )
    parser.add_argument(
        "--no-verification-loop", action="store_true",
        help="A/B 对照组：关闭验证循环，保持 v0.8.2 单轮行为。用于同实例同模型对比。",
    )
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--traces", type=Path, required=True)
    parser.add_argument(
        "--resume-completed", action="store_true",
        help="Reuse a validated .xenon_result.json already written by this run.",
    )
    args = parser.parse_args()
    namespace = None if args.namespace.lower() == "none" else args.namespace
    ids = set(args.instance_id)
    data = load_dataset(args.dataset, split=args.split)
    instances = [dict(row) for row in data if row["instance_id"] in ids]
    if len(instances) != len(ids):
        found = {x["instance_id"] for x in instances}
        raise SystemExit(f"unknown official instance(s): {sorted(ids - found)}")
    if args.min_request_interval < 0:
        parser.error("--min-request-interval must be non-negative")
    if args.provider_attempts < 1:
        parser.error("--provider-attempts must be at least 1")
    engines = list(ALL_ENGINES if "all" in args.engines else args.engines)
    if not args.include_non_editing:
        excluded = [e for e in engines if e in _NON_CODE_EDITING]
        if excluded:
            print(
                f"排除不修改文件系统的引擎（SWE-bench 代码编辑评测不适用）: "
                f"{', '.join(excluded)}。用 --include-non-editing 显式包含。"
            )
            engines = [e for e in engines if e not in _NON_CODE_EDITING]
    if not engines:
        raise SystemExit("没有可运行的引擎（全部被排除）。")
    results = []
    # Children chdir into the official task worktree.  Resolve before spawn so
    # every lifecycle event remains in the requested report directory.
    events_path = args.traces.with_suffix(".events.jsonl").resolve()
    events_path.unlink(missing_ok=True)
    original = Path.cwd()
    try:
        try:
            for instance in instances:
                _append_event(events_path, "task_start",
                              instance_id=instance["instance_id"])
                _prepare_with_timeout(
                    instance, args.prepared_root, namespace,
                    args.prepare_timeout, events_path,
                )
            for instance in instances:
                for name in engines:
                    os.chdir(original)
                    completed_path = (
                        args.prepared_root / instance["instance_id"] / name
                        / ".xenon_result.json"
                    )
                    expected_model = f"xenon/{name}/{args.model}"
                    if args.resume_completed and completed_path.exists():
                        completed = json.loads(completed_path.read_text())
                        if (
                            completed.get("instance_id") != instance["instance_id"]
                            or completed.get("engine") != name
                            or completed.get("model_name_or_path") != expected_model
                        ):
                            raise RuntimeError(
                                f"stale completed result does not match run: {completed_path}"
                            )
                        # A provider outage, timeout, or bootstrap error is a
                        # checkpoint, not a completed capability result.
                        if completed.get("error") is None:
                            results.append(completed)
                            _append_event(
                                events_path, "completed_result_recovered",
                                instance_id=instance["instance_id"], engine=name,
                                result_path=str(completed_path),
                            )
                            continue
                        _append_event(
                            events_path, "failed_result_not_recovered",
                            instance_id=instance["instance_id"], engine=name,
                            result_path=str(completed_path),
                            error=str(completed.get("error"))[:500],
                        )
                    kwargs = {
                        "instance": instance,
                        "engine_name": name,
                        "root": args.prepared_root,
                        "model": args.model,
                        "max_steps": args.max_steps,
                        "request_timeout": args.request_timeout,
                        "engine_timeout": args.engine_timeout,
                        "min_request_interval": args.min_request_interval,
                        "provider_attempts": args.provider_attempts,
                        "events_path": events_path,
                        "namespace": namespace,
                        "verification_loop": not args.no_verification_loop,
                    }
                    results.append(_run_with_hard_timeout(
                        kwargs, args.engine_timeout + 15, events_path
                    ))
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
                      "traces": str(args.traces),
                      "events": str(events_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
