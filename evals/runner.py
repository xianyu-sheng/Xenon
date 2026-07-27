"""Run Xenon mock or real-model evals and write a Markdown report."""

from __future__ import annotations

import argparse
import os
import shutil
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import yaml

try:
    from evals.mock_agent import MockAgent, estimate_tokens
except ImportError:  # pragma: no cover - script execution fallback
    from mock_agent import MockAgent, estimate_tokens


DEFAULT_TASKS_PATH = Path(__file__).with_name("tasks.yaml")
DEFAULT_REPORT_PATH = Path(__file__).parent / "reports" / "mock_report.md"


@contextmanager
def isolated_eval_credentials(
    workdir: str | Path,
    source_path: str | Path | None = None,
):
    """Run an eval with provider/MCP persistence redirected into ``workdir``.

    Real evals execute tools that may persist MCP servers or model settings. Copy
    the user's credentials into a mode-0600 file under the disposable workdir,
    then point Xenon's provider registry at that file before importing the runtime.
    The source file is never modified and the environment override is restored.
    """
    source = Path(
        source_path
        or os.environ.get("XENON_CREDENTIALS_PATH", str(Path.home() / ".xenon" / "credentials.yaml")),
    ).expanduser()
    target = Path(workdir) / ".xenon" / "credentials.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        target.write_bytes(source.read_bytes())
    else:
        target.write_text("{}\n", encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass

    previous = os.environ.get("XENON_CREDENTIALS_PATH")
    os.environ["XENON_CREDENTIALS_PATH"] = str(target)
    try:
        yield target
    finally:
        if previous is None:
            os.environ.pop("XENON_CREDENTIALS_PATH", None)
        else:
            os.environ["XENON_CREDENTIALS_PATH"] = previous


@contextmanager
def _change_directory(path: str):
    """Python 3.10-compatible equivalent of ``contextlib.chdir``."""
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def load_tasks(path: str | Path = DEFAULT_TASKS_PATH) -> list[dict[str, Any]]:
    """Load and validate eval tasks from YAML."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    tasks = data.get("tasks", data)
    if not isinstance(tasks, list):
        raise ValueError("Eval tasks must be a list or a mapping with a 'tasks' list.")
    for task in tasks:
        validate_task(task)
    return tasks


def validate_task(task: dict[str, Any]) -> None:
    # success_criteria 不再必填（§8.14.4：原实现只做工具名包含检查，criteria 形同虚设；
    # 改为可选的人类复核提示，不参与自动评分）。保留字段供报告展示与人工 review。
    required = {"id", "category", "prompt", "expected_tools"}
    missing = required - set(task)
    if missing:
        raise ValueError(f"Task is missing required fields: {sorted(missing)}")
    if not isinstance(task["expected_tools"], list):
        raise ValueError(f"Task {task['id']} expected_tools must be a list.")
    assertions = task.get("assertions")
    if assertions is not None and not isinstance(assertions, dict):
        raise ValueError(f"Task {task['id']} assertions must be a mapping.")


def evaluate_assertions(
    task: dict[str, Any],
    answer: str,
    *,
    workdir: str | Path | None = None,
) -> dict[str, Any]:
    """Evaluate privacy-safe, deterministic task result assertions.

    Assertions are deliberately small and data-oriented.  They never inspect
    prompts or credentials and are optional so legacy tasks remain runnable.
    Supported keys:

    ``files_exist``
        Relative paths that must exist below the eval workdir.
    ``files_contain`` / ``files_not_contain``
        Mapping of relative path to one string or a list of strings.
    ``answer_contains`` / ``answer_not_contains`` / ``answer_contains_any``
        Literal checks against the final answer.
    """
    assertions = task.get("assertions")
    if not assertions:
        return {"configured": False, "passed": None, "checks": [], "failures": []}
    if not isinstance(assertions, dict):
        return {"configured": True, "passed": False, "checks": [], "failures": [
            "assertions must be a mapping",
        ]}

    root = Path(workdir or Path.cwd()).resolve()
    checks: list[str] = []
    failures: list[str] = []

    def safe_path(raw: Any) -> Path | None:
        candidate = Path(str(raw))
        if candidate.is_absolute():
            failures.append(f"absolute assertion path is not allowed: {candidate}")
            return None
        resolved = (root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            failures.append(f"assertion path escapes workdir: {candidate}")
            return None
        return resolved

    def strings(value: Any) -> list[str]:
        values = value if isinstance(value, list) else [value]
        return [str(item) for item in values]

    for raw_path in strings(assertions.get("files_exist", [])):
        path = safe_path(raw_path)
        if path is None:
            continue
        if path.is_file():
            checks.append(f"file exists: {raw_path}")
        else:
            failures.append(f"missing file: {raw_path}")

    for key, negate in (("files_contain", False), ("files_not_contain", True)):
        mapping = assertions.get(key, {})
        if not isinstance(mapping, dict):
            failures.append(f"{key} must be a mapping")
            continue
        for raw_path, expected in mapping.items():
            path = safe_path(raw_path)
            if path is None:
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                failures.append(f"cannot read assertion file: {raw_path}")
                continue
            for needle in strings(expected):
                present = needle in content
                valid = not present if negate else present
                label = "absent from" if negate else "present in"
                if valid:
                    checks.append(f"{needle!r} {label} {raw_path}")
                else:
                    failures.append(f"{needle!r} not {label} {raw_path}")

    final_answer = str(answer or "")
    for key, negate in (("answer_contains", False), ("answer_not_contains", True)):
        for needle in strings(assertions.get(key, [])):
            present = needle in final_answer
            valid = not present if negate else present
            if valid:
                checks.append(f"answer {'omits' if negate else 'contains'} {needle!r}")
            else:
                failures.append(f"answer {'contains forbidden' if negate else 'does not contain'} {needle!r}")
    any_needles = strings(assertions.get("answer_contains_any", []))
    if any_needles:
        matched = [needle for needle in any_needles if needle in final_answer]
        if matched:
            checks.append(f"answer contains one of {any_needles!r}")
        else:
            failures.append(f"answer contains none of {any_needles!r}")

    return {
        "configured": True,
        "passed": not failures,
        "checks": checks,
        "failures": failures,
    }


class RealAgent:
    """真实引擎 eval agent（§8.14.2 修复）：跑 ReAct 多轮闭环，按**实际执行**
    的工具评分，而非单轮裸 LLM 列工具名。

    - 在可选 ``workdir`` 下运行（``contextlib.chdir``），避免工具执行污染真实文件系统；
    - 通过包装 ``_execute_tool`` 记录**实际执行**的工具（收束阶段被门控的工具不计）；
    - 评分：``expected_tools ⊆ executed`` 且 final_answer 非空。``success_criteria``
      不自动评分（语义化标准无法通用机器判定），仅作人类复核提示写入报告；
    - ``engine_factory`` 可注入便于单测（默认构建 ``ReActEngine``）。

    **multi-turn 支持**（方案 C 根因 1 修复）：``max_turns`` 控制外部轮次，每轮 new
    ReActEngine 共享同一个 ``ContextManager`` 累积 user/assistant 消息，前一轮
    ``answer`` 注入后一轮 user_input 作为 review feedback。**通用机制**改进——
    不针对特定任务加白名单；不修改评分逻辑；不修改 expected_tools 列表。
    """

    def __init__(
        self,
        model: str,
        *,
        max_iterations: int = 8,
        max_turns: int = 3,
        workdir: str | None = None,
        isolate_tasks: bool = False,
        engine_factory: Any = None,
    ) -> None:
        self.model = model
        self.max_iterations = max_iterations
        self.max_turns = max_turns
        self.workdir = workdir
        self.isolate_tasks = isolate_tasks
        self._engine_factory = engine_factory

    def _prepare_task_workdir(self, task: dict[str, Any]) -> str | None:
        """Create a clean per-task copy while preserving the fixture baseline."""
        if not self.workdir or not self.isolate_tasks:
            return self.workdir
        root = Path(self.workdir).resolve()
        task_root = root / "tasks" / str(task["id"])
        task_root.mkdir(parents=True, exist_ok=True)
        excluded = {"tasks", ".git"}
        for source in root.iterdir():
            if source.name in excluded:
                continue
            target = task_root / source.name
            if source.name == ".xenon":
                target.mkdir(exist_ok=True)
                for child in source.iterdir():
                    if child.name in {"credentials.yaml", "sessions"}:
                        continue
                    if child.is_dir():
                        shutil.copytree(child, target / child.name, dirs_exist_ok=True)
                    else:
                        shutil.copy2(child, target / child.name)
            elif source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            elif source.is_file():
                shutil.copy2(source, target)
        return str(task_root)

    def _default_engine_factory(self, callback: Any) -> Any:
        from xenon.engine.react_engine import ReActEngine

        return ReActEngine(
            [self.model], max_iterations=self.max_iterations, callback=callback,
        )

    def _build_context(self) -> Any:
        from xenon.engine.context import AgentContext

        return AgentContext()

    def _synthesize_review_prompt(
        self, original_prompt: str, prev_answer: str, turn: int,
    ) -> str:
        """生成第 N 轮的 review feedback prompt（通用机制，不针对特定任务）。

        第 1 轮用原任务；后续轮基于前一轮 answer + 原任务，让 LLM 自然产生
        '修订/补充/再确认' 行为，覆盖 multi_turn_revision 类任务。
        """
        return (
            f"Continue based on your previous answer (turn {turn}):\n"
            f"{prev_answer[:500]}\n\n"
            f"Original task: {original_prompt}"
        )

    def run_task(self, task: dict[str, Any]) -> dict[str, Any]:
        from xenon.repl.context_manager import ContextManager
        factory = self._engine_factory or self._default_engine_factory
        executed: list[str] = []
        answer = ""
        expected = set(task.get("expected_tools", []))
        original_prompt = task["prompt"]

        # multi-turn：每轮共享 ContextManager 累积 history（F4 修复机制）
        cm = ContextManager()
        turns_used = 1
        callback_telemetry = _EvalTelemetryCallback()
        task_workdir = self._prepare_task_workdir(task)
        try:
            with _change_directory(task_workdir) if task_workdir else nullcontext():
                for turn in range(self.max_turns):
                    turns_used = turn + 1
                    if turn == 0:
                        user_input = original_prompt
                    else:
                        user_input = self._synthesize_review_prompt(
                            original_prompt, answer, turn,
                        )
                    cm.add_user_message(user_input)

                    eng = factory(callback_telemetry)
                    # 包装 _execute_tool 记录实际执行的工具（门控/拦截的不计）
                    orig_execute = eng._execute_tool

                    def _recording_execute(action, action_input, ctx, tracker=None):
                        executed.append(action)
                        return orig_execute(action, action_input, ctx, tracker)

                    eng._execute_tool = _recording_execute
                    # F4：ctx_mgr 注入，engine 消费（已压缩）历史
                    try:
                        answer = eng.run(user_input, ctx_mgr=cm) or ""
                    finally:
                        # Test doubles and injected engines may be reused for
                        # multiple turns; do not wrap an already wrapped
                        # method and create a recursive closure chain.
                        eng._execute_tool = orig_execute
                    cm.add_assistant_message(answer)

                    # 早停：所有 expected_tools 都调过了（无需进入下一轮）
                    if expected and expected.issubset(set(executed)):
                        break
                    # 早停：第 1 轮就空 executed + 强制拒绝过 → 不浪费 token
                    if turn == 0 and not executed:
                        break

                success, reason = self._score(task, executed, answer)
                verification = evaluate_assertions(task, answer, workdir=task_workdir)
                if success and verification["configured"] and not verification["passed"]:
                    success = False
                    reason = f"result assertions failed: {verification['failures']}"
                notes = answer.strip()[:200] or reason
        except Exception as exc:  # noqa: BLE001 — eval 不应因单任务崩溃中断
            success, reason = False, f"engine run failed: {exc}"
            notes = reason[:200]
            verification = evaluate_assertions(task, answer, workdir=task_workdir)

        missing = [t for t in expected if t not in executed]
        return {
            "task_id": task["id"],
            "category": task["category"],
            "success": success,
            "model": self.model,
            "token_count": estimate_tokens(task["prompt"]) + estimate_tokens(answer),
            "tool_calls": len(executed),
            "tool_failures": len(missing),
            "tools_used": executed,
            "notes": notes,
            "scoring": reason,
            "verification": verification,
            "turns_used": turns_used,
            "xenon_task_metrics": callback_telemetry.as_dict(),
        }

    @staticmethod
    def _score(task: dict[str, Any], executed: list[str], answer: str) -> tuple[bool, str]:
        """评分：实际执行了全部 expected_tools 且 final_answer 非空。"""
        expected = set(task.get("expected_tools", []))
        missing = expected - set(executed)
        if missing:
            return False, f"missing expected tools: {sorted(missing)}"
        if not (answer or "").strip():
            return False, "empty final answer"
        return True, f"executed all {len(expected)} expected tools"

    @staticmethod
    def _build_prompt(task: dict[str, Any]) -> str:
        """§8.14.1 修复：prompt **绝不暴露** expected_tools（仅评分用）。

        只给任务描述 + 类别 + success_criteria（作为背景，助模型理解验收标准）。
        expected_tools 仅由 runner 用于评分，不出现在 prompt 中——这样才能测出
        "模型从任务描述自主推断工具"的真实能力，而非复述喂给它的工具名。
        """
        criteria = task.get("success_criteria", "")
        lines = [
            f"Task: {task['prompt']}",
            f"Category: {task['category']}",
        ]
        if criteria:
            lines.append(f"Success criteria (for your understanding): {criteria}")
        lines.append(
            "Decide which tools to use and execute the task. The runner scores whether you "
            "actually executed the right tools—do not self-grade."
        )
        return "\n".join(lines)


class _EvalTelemetryCallback:
    """Privacy-safe callback counters for governance signals in real evals."""

    def __init__(self) -> None:
        self.tool_calls = 0
        self.observations = 0
        self.permission_denied = 0
        self.permission_cancelled = 0
        self.invalid_parameters = 0
        self.path_blocks = 0
        self.errors = 0
        self.warnings = 0

    # EngineCallback-compatible no-op hooks.  Keeping the callback duck-typed
    # avoids importing the engine module during eval discovery while still
    # satisfying every callback invocation used by ReAct/Plan engines.
    def on_think(self, thought: str) -> None:
        pass

    def on_step(self, step_id: int, total: int, task: str) -> None:
        pass

    def on_step_done(self, step_id: int, success: bool, summary: str) -> None:
        pass

    def on_review(self, score: int, passed: bool, feedback: str) -> None:
        pass

    def on_finish(self, result: str) -> None:
        pass

    def on_act(self, action: str, action_input: dict) -> None:
        self.tool_calls += 1

    def on_observe(self, observation: str) -> None:
        self.observations += 1
        text = str(observation).lower()
        if "权限拒绝" in text or "操作被拒绝" in text or "permission_denied" in text:
            self.permission_denied += 1
        if "取消任务" in text or "cancelled" in text:
            self.permission_cancelled += 1
        if "参数校验失败" in text or "参数幻觉" in text or "invalid_parameters" in text:
            self.invalid_parameters += 1
        if "路径越界" in text or "outside allowed" in text:
            self.path_blocks += 1

    def on_error(self, error: str) -> None:
        self.errors += 1

    def on_warning(self, warning: str) -> None:
        self.warnings += 1

    def as_dict(self) -> dict[str, int]:
        return {
            "tool_calls": self.tool_calls,
            "observations": self.observations,
            "permission_denied": self.permission_denied,
            "permission_cancelled": self.permission_cancelled,
            "invalid_parameters": self.invalid_parameters,
            "path_blocks": self.path_blocks,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class XenonMetrics:
    """Aggregate Xenon-specific value signals without changing task scoring.

    ``success_rate`` remains the general agent capability score.  This class
    reports the independent signals that make Xenon useful in production:
    provider-reported cache rails, token/cost savings, routing evidence and
    the observability coverage of governance features.  Unknown values are
    represented by ``None`` rather than being silently counted as zero.
    """

    @staticmethod
    def _round(value: float | int | None, digits: int = 4) -> float | None:
        return round(float(value), digits) if value is not None else None

    @classmethod
    def from_runtime(
        cls,
        results: list[dict[str, Any]],
        *,
        usage: dict[str, dict[str, Any]] | None = None,
        cache: dict[str, Any] | None = None,
        primary_model: str | None = None,
    ) -> dict[str, Any]:
        usage = usage or {}
        cache = cache or {}
        calls = sum(int(item.get("calls", 0) or 0) for item in usage.values())
        prompt = sum(int(item.get("prompt_tokens", 0) or 0) for item in usage.values())
        completion = sum(int(item.get("completion_tokens", 0) or 0) for item in usage.values())
        total = sum(int(item.get("total_tokens", 0) or 0) for item in usage.values())
        hit = sum(int(item.get("cache_hit_tokens", 0) or 0) for item in usage.values())
        miss = sum(int(item.get("cache_miss_tokens", 0) or 0) for item in usage.values())
        latency = sum(
            float(item.get("latency_avg", 0.0) or 0.0) * int(item.get("calls", 0) or 0)
            for item in usage.values()
        )
        models = sorted(str(model) for model in usage)
        cache_calls = int(cache.get("total_calls", 0) or 0)
        cache_coverage = cache.get("cache_field_coverage")
        provider_cache = bool(cache_calls and cache_coverage and float(cache_coverage) > 0)
        events = [event for event in cache.get("events", []) if isinstance(event, dict)]
        if provider_cache:
            # CacheTracker consumes the raw provider response and is the
            # source of truth for cache accounting.  UsageTracker remains the
            # source of truth for total/completion tokens, but some native
            # tool-call paths only emit response telemetry.
            hit = int(cache.get("cache_hits", hit) or 0)
            miss = int(cache.get("cache_misses", miss) or 0)
            if cache_calls > calls:
                calls = cache_calls
                prompt = sum(int(event.get("prompt_tokens", 0) or 0) for event in events)
                completion = sum(int(event.get("completion_tokens", 0) or 0) for event in events)
                total = prompt + completion
        task_governance = [
            result.get("xenon_task_metrics", {}) for result in results
            if isinstance(result.get("xenon_task_metrics"), dict)
        ]
        governance_observed = bool(task_governance)
        permission_denied = sum(int(item.get("permission_denied", 0) or 0) for item in task_governance)
        permission_cancelled = sum(int(item.get("permission_cancelled", 0) or 0) for item in task_governance)
        invalid_parameters = sum(int(item.get("invalid_parameters", 0) or 0) for item in task_governance)
        path_blocks = sum(int(item.get("path_blocks", 0) or 0) for item in task_governance)
        rails = {
            "rail_count": len({str(event.get("cache_lane")) for event in events if event.get("cache_lane")}),
            "cache_family_count": len({str(event.get("cache_family")) for event in events if event.get("cache_family")}),
            "rail_forks": sum(1 for event in events if event.get("cause") in {
                "model_switch", "engine_switch", "phase_switch", "toolset_changed",
                "project_changed", "context_compacted", "stable_prefix_changed",
                "history_rewritten",
            }),
            "context_epoch_changes": sum(1 for event in events if event.get("cause") == "context_compacted"),
            "reusable_prefix_tokens": sum(int(event.get("cache_hit_tokens", 0) or 0) for event in events),
        }

        if provider_cache:
            actual_cost = cache.get("estimated_cost_yuan")
            saved_cost = cache.get("savings_yuan")
            baseline_cost = (
                float(actual_cost or 0) + float(saved_cost or 0)
                if actual_cost is not None and saved_cost is not None else None
            )
            savings_pct = (
                float(saved_cost) / baseline_cost * 100
                if baseline_cost and saved_cost is not None else None
            )
            cost_quality = "provider_cache_fields"
        elif calls:
            # No cache field means a conservative all-miss estimate.  It is
            # useful as a cost ceiling, but must never be labelled as savings.
            actual_cost = baseline_cost = 0.0
            try:
                from xenon.utils.deepseek_cache import CacheTracker
                pricing_tracker = CacheTracker(persist=False)
                try:
                    for model, item in usage.items():
                        pricing = pricing_tracker.get_pricing(model)
                        actual_cost += (int(item.get("prompt_tokens", 0) or 0) / 1_000_000) * pricing["input_cache_miss"]
                        actual_cost += (int(item.get("completion_tokens", 0) or 0) / 1_000_000) * pricing["output"]
                finally:
                    pricing_tracker.close()
            except Exception:  # pragma: no cover - pricing fallback only
                actual_cost = None
            baseline_cost = actual_cost
            saved_cost = savings_pct = None
            cost_quality = "all_miss_estimate_no_provider_cache_fields"
        else:
            actual_cost = baseline_cost = saved_cost = savings_pct = None
            cost_quality = "no_llm_calls"

        successful = sum(1 for result in results if result.get("success"))
        model_calls = {model: int(item.get("calls", 0) or 0) for model, item in usage.items()}
        non_primary_calls = (
            sum(count for model, count in model_calls.items() if primary_model and model != primary_model)
            if primary_model else None
        )
        return {
            "source": "provider" if calls else "unavailable",
            "observed_llm_calls": calls,
            "models_used": models,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "average_latency_seconds": cls._round(latency / calls if calls else None),
            "cache": {
                "status": "provider_reported" if provider_cache else ("unavailable" if not calls else "not_reported"),
                "field_coverage": cls._round(cache_coverage),
                "hit_tokens": hit if calls else None,
                "miss_tokens": miss if calls else None,
                "hit_rate": cls._round(hit / (hit + miss) if hit + miss else None),
                **rails,
            },
            "cost": {
                "estimated_actual_yuan": cls._round(actual_cost),
                "all_cache_miss_baseline_yuan": cls._round(baseline_cost),
                "saved_yuan": cls._round(saved_cost),
                "saved_pct": cls._round(savings_pct, 2),
                "quality": cost_quality,
            },
            "routing": {
                "primary_model": primary_model,
                "model_calls": model_calls,
                "fallback_calls_observed": non_primary_calls,
                "fallback_success_rate": None,
                "note": "失败尝试不会产生 usage 回调；需路由事件才能计算完整 fallback 成功率。",
            },
            "governance": {
                "permission_requests": (permission_denied + permission_cancelled if governance_observed else None),
                "approved": None,
                "denied": (permission_denied if governance_observed else None),
                "cancelled": (permission_cancelled if governance_observed else None),
                "invalid_parameter_blocks": (invalid_parameters if governance_observed else None),
                "path_blocks": (path_blocks if governance_observed else None),
                "status": "observed_tool_governance" if governance_observed else "not_instrumented_in_this_eval",
            },
            "memory_recovery": {
                "memory_writes_confirmed": None,
                "memory_retrieval_hits": None,
                "session_resume_success": None,
                "status": "not_instrumented_in_this_eval",
            },
            "efficiency": {
                "tokens_per_successful_task": cls._round(total / successful if successful else None, 2),
                "cost_per_successful_task_yuan": cls._round(float(actual_cost) / successful if actual_cost is not None and successful else None),
            },
        }


def run_eval(
    tasks: list[dict[str, Any]],
    *,
    mode: str,
    model: str | None = None,
    workdir: str | None = None,
    isolate_tasks: bool = False,
) -> list[dict[str, Any]]:
    """Run tasks through mock or real agent."""
    usage_tracker = cache_tracker = None
    if mode == "mock":
        agent = MockAgent()
    elif mode == "real":
        if not model:
            raise ValueError("--model is required when --mode real")
        agent = RealAgent(model, workdir=workdir, isolate_tasks=isolate_tasks)
        # Subscribe once around the complete run.  These are existing Xenon
        # telemetry sources; no prompt or credential content is persisted.
        from xenon.utils.deepseek_cache import CacheTracker
        from xenon.utils.llm_client import UsageTracker
        usage_tracker = UsageTracker()
        cache_tracker = CacheTracker(persist=False)
    else:
        raise ValueError(f"Unsupported eval mode: {mode}")
    results: list[dict[str, Any]] = []
    try:
        results = [agent.run_task(task) for task in tasks]
    finally:
        if usage_tracker is not None and cache_tracker is not None:
            runtime = {
                "usage": usage_tracker.snapshot(),
                "cache": {
                    "total_calls": cache_tracker.total_calls,
                    "cache_field_coverage": cache_tracker.cache_field_coverage,
                    "cache_hits": cache_tracker.cache_hits,
                    "cache_misses": cache_tracker.cache_misses,
                    "estimated_cost_yuan": cache_tracker.estimated_cost_yuan,
                    "savings_yuan": cache_tracker.savings_yuan,
                    "events": cache_tracker.recent_events(10000),
                },
            }
            metrics = XenonMetrics.from_runtime(
                results,
                usage=runtime["usage"],
                cache=runtime["cache"],
                primary_model=model,
            )
            for result in results:
                result["_xenon_metrics"] = metrics
            usage_tracker.close()
            cache_tracker.close()
    return results


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    successes = sum(1 for result in results if result["success"])
    verified = [
        result for result in results
        if result.get("verification", {}).get("configured")
    ]
    verified_successes = sum(
        1 for result in verified
        if result.get("success") and result.get("verification", {}).get("passed")
    )
    return {
        "tasks": total,
        "successes": successes,
        "success_rate": (successes / total * 100) if total else 0.0,
        "average_tokens": mean(result["token_count"] for result in results) if results else 0,
        "tool_calls": sum(result["tool_calls"] for result in results),
        "tool_failures": sum(result["tool_failures"] for result in results),
        "verified_tasks": len(verified),
        "verified_successes": verified_successes,
        "verification_rate": (verified_successes / len(verified) * 100) if verified else None,
    }


def write_report(
    results: list[dict[str, Any]],
    output_path: str | Path,
    *,
    mode: str,
    model: str,
    run_date: str | None = None,
) -> Path:
    """Write a Markdown eval report and return the path."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = summarize(results)
    xenon_metrics = results[0].get("_xenon_metrics") if results else None
    if xenon_metrics is None:
        xenon_metrics = XenonMetrics.from_runtime(results, primary_model=model if mode == "real" else None)
    date = run_date or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        "# Xenon Eval Report",
        "",
    ]
    # §8.14.3 修复：mock 模式显式标注为框架自检，不代表 agent 能力
    if mode == "mock":
        lines.extend([
            "> ⚠️ **Framework smoke test — NOT an agent capability measurement.**",
            "> mock 模式仅验证 eval 框架自身能跑通 + YAML 可解析，success_rate 恒 100%，",
            "> 与模型/引擎能力无关。判断 agent 能力请用 `--mode real`。",
            "",
        ])
    elif mode == "real":
        lines.extend([
            "> Scoring: real 模式跑 ReAct 多轮闭环，按**实际执行**的工具评分",
            ">（`expected_tools ⊆ executed` 且 final_answer 非空）；配置了 `assertions` 的任务还会执行结果断言。`success_criteria` 仍为人工复核提示。",
            "",
        ])
    lines.extend([
        f"- Mode: `{mode}`",
        f"- Model: `{model}`",
        f"- Run date: `{date}`",
        f"- Tasks: {summary['tasks']}",
        f"- Success Rate: {summary['success_rate']:.1f}%",
        f"- Average Tokens: {summary['average_tokens']:.1f}",
        f"- Tool Calls: {summary['tool_calls']}",
        f"- Tool Failures: {summary['tool_failures']}",
        f"- Verified Tasks: {summary['verified_tasks']}/{summary['tasks']}",
        f"- Verified Success Rate: {_display_metric(summary['verification_rate'], suffix='%')}",
        "",
        "| Task | Category | Success | Verified | Tokens | Tool Calls | Tool Failures | Notes |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | --- |",
    ])
    for result in results:
        notes = str(result.get("notes", "")).replace("\n", " ")[:140]
        success = "yes" if result["success"] else "no"
        verification = result.get("verification", {})
        verified = "yes" if verification.get("configured") and verification.get("passed") else (
            "failed" if verification.get("configured") else "n/a"
        )
        lines.append(
            f"| `{result['task_id']}` | {result['category']} | {success} | {verified} | "
            f"{result['token_count']} | {result['tool_calls']} | {result['tool_failures']} | {notes} |"
        )

    lines.extend([
        "",
        "## Xenon-Specific Value",
        "",
        "> These metrics are separate from task success rate. `N/A` means the signal was not observable in this run; it is never treated as zero.",
        "",
        "### Cache Rails and Cost",
        "",
        f"- Provider cache telemetry: **{xenon_metrics['cache']['status']}**",
        f"- Cache field coverage: {_display_metric(xenon_metrics['cache']['field_coverage'], suffix='%', scale=100)}",
        f"- Cache hit rate: {_display_metric(xenon_metrics['cache']['hit_rate'], suffix='%', scale=100)}",
        f"- Reusable prefix / hit tokens: {_display_metric(xenon_metrics['cache']['reusable_prefix_tokens'])}",
        f"- Cache rails: {_display_metric(xenon_metrics['cache']['rail_count'])}; rail forks: {_display_metric(xenon_metrics['cache']['rail_forks'])}; context compactions: {_display_metric(xenon_metrics['cache']['context_epoch_changes'])}",
        f"- Estimated actual cost: {_display_currency(xenon_metrics['cost']['estimated_actual_yuan'])}",
        f"- All-cache-miss baseline: {_display_currency(xenon_metrics['cost']['all_cache_miss_baseline_yuan'])}",
        f"- Estimated savings: {_display_currency(xenon_metrics['cost']['saved_yuan'])} ({_display_metric(xenon_metrics['cost']['saved_pct'], suffix='%')})",
        f"- Cost evidence: `{xenon_metrics['cost']['quality']}`",
        "",
        "### Routing, Governance and Recovery",
        "",
        f"- Models observed: {', '.join(xenon_metrics['models_used']) or 'N/A'}",
        f"- Fallback calls observed: {_display_metric(xenon_metrics['routing']['fallback_calls_observed'])}",
        f"- Fallback success rate: {_display_metric(xenon_metrics['routing']['fallback_success_rate'], suffix='%')}",
        f"- Permission telemetry: `{xenon_metrics['governance']['status']}`",
        f"- Permission denied/cancelled: {_display_metric(xenon_metrics['governance']['denied'])}/{_display_metric(xenon_metrics['governance']['cancelled'])}; invalid-parameter blocks: {_display_metric(xenon_metrics['governance']['invalid_parameter_blocks'])}; path blocks: {_display_metric(xenon_metrics['governance']['path_blocks'])}",
        f"- Memory/recovery telemetry: `{xenon_metrics['memory_recovery']['status']}`",
        "",
        "### Efficiency",
        "",
        f"- Observed LLM calls: {_display_metric(xenon_metrics['observed_llm_calls'])}",
        f"- Tokens per successful task: {_display_metric(xenon_metrics['efficiency']['tokens_per_successful_task'])}",
        f"- Cost per successful task: {_display_currency(xenon_metrics['efficiency']['cost_per_successful_task_yuan'])}",
    ])

    failures = [result for result in results if not result["success"]]
    if failures:
        lines.extend(["", "## Failure Summary", ""])
        for result in failures:
            lines.append(f"- `{result['task_id']}`: {result.get('notes', '')}")

    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def _display_metric(value: Any, *, suffix: str = "", scale: float = 1.0) -> str:
    if value is None:
        return "N/A"
    number = float(value) * scale
    if number.is_integer():
        rendered = str(int(number))
    else:
        rendered = f"{number:.2f}"
    return rendered + suffix


def _display_currency(value: Any) -> str:
    return "N/A" if value is None else f"¥{float(value):.4f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Xenon evals.")
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
    parser.add_argument("--model", default=None, help="Required for --mode real, e.g. deepseek/deepseek-v4-pro")
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS_PATH))
    parser.add_argument("--output", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument(
        "--credentials-path", default=None,
        help="Optional source credentials file; real evals copy it into the isolated workdir.",
    )
    parser.add_argument(
        "--workdir", default=None,
        help="Optional working directory for --mode real (tool execution sandbox).",
    )
    parser.add_argument(
        "--isolate-tasks", action="store_true",
        help="Copy the fixture baseline into a clean subdirectory for each real task.",
    )
    args = parser.parse_args(argv)

    tasks = load_tasks(args.tasks)
    if args.mode == "real" and not args.workdir:
        parser.error("--workdir is required for --mode real so file and credential writes stay isolated")
    credentials_context = (
        isolated_eval_credentials(args.workdir, args.credentials_path)
        if args.mode == "real" and args.workdir
        else nullcontext()
    )
    with credentials_context:
        results = run_eval(
            tasks, mode=args.mode, model=args.model, workdir=args.workdir,
            isolate_tasks=args.isolate_tasks,
        )
    model = args.model or "mock-agent"
    report = write_report(results, args.output, mode=args.mode, model=model)
    print(f"Wrote eval report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
