"""Run Xenon mock or real-model evals and write a Markdown report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
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
DEFAULT_REPL_TASKS_PATH = Path(__file__).with_name("repl_tasks.yaml")
DEFAULT_REPORT_PATH = Path(__file__).parent / "reports" / "mock_report.md"
SUPPORTED_ENGINE_TYPES = (
    "direct", "react", "plan-execute", "reflection", "plan-react",
    "plan-reflection", "react-reflection",
)


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


class ReplCommandAgent:
    """Execute slash commands through Xenon's actual REPL command path.

    This suite is intentionally separate from ReAct tool-choice scoring: a
    command is successful only when the REPL dispatcher returns a non-error
    result and its command-specific assertions pass.
    """

    def __init__(self, *, workdir: str | None = None) -> None:
        self.workdir = workdir

    def run_task(self, task: dict[str, Any]) -> dict[str, Any]:
        from unittest.mock import patch
        from xenon.repl.commands import dispatch_command
        from xenon.repl.repl import REPL
        from xenon.utils.deepseek_cache import CacheTracker

        answer = ""
        try:
            with _change_directory(self.workdir) if self.workdir else nullcontext():
                # REPL normally persists cache history.  The command suite is
                # disposable, so force an in-memory tracker and keep the user's
                # ~/.xenon state untouched.
                with patch(
                    "xenon.utils.deepseek_cache.CacheTracker",
                    lambda *args, **kwargs: CacheTracker(persist=False),
                ):
                    repl = REPL(streaming=False, optimize_prompts=False)
                raw = str(task["command"]).strip()
                parts = raw.split(maxsplit=1)
                answer = str(dispatch_command(
                    parts[0], parts[1] if len(parts) > 1 else "",
                    registry=repl.registry,
                    ctx_mgr=repl.ctx_mgr,
                    session_state=repl._session_state,
                ) or "")
                verification = evaluate_assertions(task, answer, workdir=self.workdir)
                success = bool(answer.strip()) and (
                    not verification["configured"] or verification["passed"]
                )
                reason = "REPL command returned output"
                if not success:
                    reason = "REPL command result assertions failed"
        except Exception as exc:  # noqa: BLE001 — one command must not abort suite
            success = False
            reason = f"REPL command failed: {exc}"
            verification = evaluate_assertions(task, answer, workdir=self.workdir)
        return {
            "task_id": task["id"],
            "category": "repl_command",
            "success": success,
            "model": "xenon-repl",
            "token_count": estimate_tokens(str(task.get("command", ""))) + estimate_tokens(answer),
            "tool_calls": 0,
            "tool_failures": 0,
            "tools_used": [],
            "successful_tools": [],
            "tool_events": [],
            "notes": answer.strip()[:200] or reason,
            "scoring": reason,
            "verification": verification,
            "turns_used": 1,
            "xenon_task_metrics": {},
        }


class _DirectEvalEngine:
    """Answer-only baseline matching Xenon's direct REPL mode."""

    def __init__(self, model: str, callback: Any = None) -> None:
        self.model = model
        self.callback = callback

    def run(self, user_input: str, context: Any = None, ctx_mgr: Any = None) -> str:
        from xenon.utils.llm_client import chat_completion

        messages = ctx_mgr.get_messages() if ctx_mgr is not None else [
            {"role": "user", "content": user_input},
        ]
        return chat_completion(self.model, messages, max_tokens=1024, temperature=0.3)


def validate_task(task: dict[str, Any]) -> None:
    if "command" in task:
        required = {"id", "command"}
        missing = required - set(task)
        if missing:
            raise ValueError(f"REPL task is missing required fields: {sorted(missing)}")
        if not isinstance(task["command"], str) or not task["command"].startswith("/"):
            raise ValueError(f"REPL task {task['id']} command must be a slash command.")
        assertions = task.get("assertions")
        if assertions is not None and not isinstance(assertions, dict):
            raise ValueError(f"Task {task['id']} assertions must be a mapping.")
        return
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
    ``commands_pass``
        Fixture-authored commands that must exit successfully in the isolated
        task directory (never commands supplied by the model).
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

    commands = assertions.get("commands_pass", [])
    if commands and not isinstance(commands, list):
        failures.append("commands_pass must be a list")
    for command in commands if isinstance(commands, list) else []:
        try:
            completed = subprocess.run(
                str(command), cwd=root, shell=True, capture_output=True,
                text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"command failed to run: {command!r} ({exc})")
            continue
        if completed.returncode == 0:
            checks.append(f"command passed: {command!r}")
        else:
            detail = (completed.stdout + completed.stderr).strip().splitlines()
            suffix = f": {detail[-1][:160]}" if detail else ""
            failures.append(f"command failed ({completed.returncode}): {command!r}{suffix}")

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
        engine_type: str = "react",
        max_iterations: int = 8,
        max_turns: int = 3,
        request_timeout: float = 30.0,
        workdir: str | None = None,
        isolate_tasks: bool = False,
        engine_factory: Any = None,
    ) -> None:
        self.model = model
        self.engine_type = engine_type
        self.max_iterations = max_iterations
        self.max_turns = max_turns
        self.request_timeout = request_timeout
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
        fixture = task.get("fixture")
        fixture_root = (
            Path(__file__).with_name("fixtures") / str(fixture)
            if fixture else None
        )
        if fixture_root is not None and not fixture_root.is_dir():
            raise ValueError(f"Task fixture does not exist: {fixture_root}")
        if fixture_root is not None:
            # A fixture is the task's complete baseline.  Copy it into a clean
            # task directory so target paths and cwd are unambiguous.
            for source in fixture_root.iterdir():
                target = task_root / source.name
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(source, target)
            return str(task_root)
        for source in root.iterdir():
            if source.name in excluded:
                continue
            target = task_root / source.name
            if source.name == ".xenon":
                target.mkdir(exist_ok=True)
                for child in source.iterdir():
                    if child.name == "credentials.yaml":
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
        common = {"model_priority": [self.model], "callback": callback}
        if self.engine_type == "direct":
            return _DirectEvalEngine(self.model, callback)
        if self.engine_type == "react":
            from xenon.engine.react_engine import ReActEngine
            return ReActEngine(
                **common, max_iterations=self.max_iterations,
            )
        if self.engine_type == "plan-execute":
            from xenon.engine.plan_execute_engine import PlanExecuteEngine
            return PlanExecuteEngine(**common, max_steps=self.max_iterations)
        if self.engine_type == "reflection":
            from xenon.engine.reflection_engine import ReflectionEngine
            return ReflectionEngine(**common, max_rounds=min(3, self.max_iterations))
        if self.engine_type == "plan-react":
            from xenon.engine.combined_engines import PlanReactEngine
            return PlanReactEngine(
                **common, max_steps=self.max_iterations,
                react_iterations=self.max_iterations,
            )
        if self.engine_type == "plan-reflection":
            from xenon.engine.combined_engines import PlanReflectionEngine
            return PlanReflectionEngine(
                **common, max_steps=self.max_iterations, review_rounds=2,
            )
        if self.engine_type == "react-reflection":
            from xenon.engine.combined_engines import ReactReflectionEngine
            return ReactReflectionEngine(
                **common, react_iterations=self.max_iterations, review_rounds=2,
            )
        raise ValueError(
            f"Unsupported engine_type {self.engine_type!r}; expected one of "
            "direct, react, plan-execute, reflection, plan-react, plan-reflection, "
            "react-reflection"
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
        successful_tools: list[str] = []
        tool_events: list[dict[str, Any]] = []
        answer = ""
        expected = set(task.get("expected_tools", []))
        original_prompt = task["prompt"]

        # multi-turn：每轮共享 ContextManager 累积 history（F4 修复机制）
        cm = ContextManager()
        turns_used = 1
        callback_telemetry = _EvalTelemetryCallback()
        task_workdir = self._prepare_task_workdir(task)
        started_at = datetime.now(timezone.utc).isoformat()
        callback_telemetry.record("task_start", task_id=task["id"], engine=self.engine_type)
        try:
            with _change_directory(task_workdir) if task_workdir else nullcontext():
                for turn in range(self.max_turns):
                    turns_used = turn + 1
                    if turn == 0:
                        user_input = self._build_prompt(task)
                    else:
                        user_input = self._synthesize_review_prompt(
                            original_prompt, answer, turn,
                        )
                    cm.add_user_message(user_input)

                    eng = factory(callback_telemetry)
                    callback_telemetry.record("engine_created", turn=turn + 1)
                    # Record attempted tools separately from successful execution.
                    # The ToolExecutor result is the source of truth; merely
                    # entering _execute_tool must never count as success.
                    orig_execute = getattr(eng, "_execute_tool", None)
                    # Combined engines own nested planner/reactor engines;
                    # instrument every engine node so their real tool results
                    # are not invisible to the evaluator.
                    engine_nodes: list[Any] = []
                    pending_nodes = [eng]
                    seen_nodes: set[int] = set()
                    while pending_nodes:
                        candidate = pending_nodes.pop(0)
                        if id(candidate) in seen_nodes:
                            continue
                        seen_nodes.add(id(candidate))
                        engine_nodes.append(candidate)
                        for attr in ("planner", "reactor", "reflector", "executor"):
                            child = getattr(candidate, attr, None)
                            if child is not None:
                                pending_nodes.append(child)
                    for node in engine_nodes:
                        # BaseEngine reads this optional per-run timeout when
                        # constructing provider requests. It prevents one
                        # stalled provider response from blocking a matrix.
                        node.request_timeout = self.request_timeout

                    def _snapshot_target(action: str, params: dict[str, Any]) -> str | None:
                        if action not in {
                            "write_file", "edit_file", "create_directory",
                            "batch_write", "batch_edit", "append_file", "refactor",
                        }:
                            return None
                        raw = params.get("file_path") or params.get("path")
                        if not raw:
                            return None
                        target = Path(str(raw))
                        if not target.is_absolute():
                            target = Path.cwd() / target
                        target = target.resolve()
                        try:
                            if target.is_file():
                                return "file:" + hashlib.sha256(target.read_bytes()).hexdigest()
                            if target.is_dir():
                                return "dir:" + ",".join(sorted(p.name for p in target.iterdir()))
                            return "missing"
                        except OSError:
                            return None

                    def _record_result(action: str, params: dict[str, Any], result: Any, before: str | None) -> None:
                        if isinstance(result, dict):
                            success = bool(result.get("success", False))
                            state = str(result.get("state") or ("succeeded" if success else "failed"))
                            error = result.get("error")
                            lifecycle = result.get("lifecycle", ()) or ()
                            attempts = int(result.get("attempts", 1) or 1)
                        else:
                            success = bool(getattr(result, "success", False))
                            state = getattr(getattr(result, "state", None), "value", None)
                            error = getattr(result, "error", None)
                            lifecycle = getattr(result, "lifecycle", ()) or ()
                            attempts = int(getattr(result, "attempts", 1) or 1)
                        after = _snapshot_target(action, params)
                        checkpoint = lifecycle[-1] if lifecycle else {}
                        if success:
                            successful_tools.append(action)
                        tool_events.append({
                            "tool": action,
                            "success": success,
                            "state": state or ("succeeded" if success else "failed"),
                            "error": str(error) if error else None,
                            "attempts": attempts,
                            "state_changed": (before != after) if before is not None and after is not None else None,
                            "confirmation_required": checkpoint.get("requires_confirmation"),
                            "confirmation_outcome": (
                                "denied" if checkpoint.get("error_kind") in {"permission_denied", "policy_denied"}
                                else "cancelled" if checkpoint.get("error_kind") == "cancelled"
                                else "approved_or_not_required" if success else "not_executed"
                            ),
                        })

                    executor_hooks: list[tuple[Any, Any]] = []
                    execute_hooks: list[tuple[Any, Any]] = []
                    for node in engine_nodes:
                        executor = getattr(node, "_tool_executor", None)
                        original = getattr(executor, "execute", None)
                        if executor is None or not callable(original):
                            continue

                        def _recording_executor(action, params, context, tracker=None, _original=original, **kwargs):
                            before = _snapshot_target(action, params)
                            result = _original(action, params, context, tracker=tracker, **kwargs)
                            _record_result(action, params, result, before)
                            return result

                        executor_hooks.append((executor, original))
                        executor.execute = _recording_executor

                    # Combined engines delegate actions to nested ReAct
                    # instances.  Count those attempts without replacing the
                    # top-level wrapper twice.
                    for node in engine_nodes:
                        if node is eng:
                            continue
                        original_execute = getattr(node, "_execute_tool", None)
                        if not callable(original_execute):
                            continue

                        def _recording_nested_execute(action, action_input, ctx, tracker=None, _original=original_execute):
                            executed.append(action)
                            return _original(action, action_input, ctx, tracker)

                        execute_hooks.append((node, original_execute))
                        node._execute_tool = _recording_nested_execute

                    has_top_executor = any(
                        node is eng for node, _original in executor_hooks
                    )

                    def _recording_execute(action, action_input, ctx, tracker=None):
                        executed.append(action)
                        if not has_top_executor:
                            successful_tools.append(action)
                            tool_events.append({
                                "tool": action, "success": True, "state": "succeeded",
                                "error": None, "attempts": 1, "state_changed": None,
                                "confirmation_required": None,
                                "confirmation_outcome": "unknown_engine_result",
                            })
                        if orig_execute is None:
                            return ""
                        return orig_execute(action, action_input, ctx, tracker)

                    if orig_execute is not None:
                        eng._execute_tool = _recording_execute
                    # F4：ctx_mgr 注入，engine 消费（已压缩）历史
                    try:
                        callback_telemetry.record("engine_run_start", turn=turn + 1)
                        answer = eng.run(user_input, ctx_mgr=cm) or ""
                        callback_telemetry.record("engine_run_end", turn=turn + 1)
                    finally:
                        # Test doubles and injected engines may be reused for
                        # multiple turns; do not wrap an already wrapped
                        # method and create a recursive closure chain.
                        if orig_execute is not None:
                            eng._execute_tool = orig_execute
                        for node, original in execute_hooks:
                            node._execute_tool = original
                        for executor, original in executor_hooks:
                            executor.execute = original
                    cm.add_assistant_message(answer)

                    # 早停：所有 expected_tools 都调过了（无需进入下一轮）
                    if expected and expected.issubset(set(executed)):
                        break
                    # 早停：第 1 轮就空 executed + 强制拒绝过 → 不浪费 token
                    if turn == 0 and not executed:
                        break

                success, reason = self._score(
                    task, executed, answer, successful_tools=successful_tools,
                    engine_type=self.engine_type,
                )
                verification = evaluate_assertions(task, answer, workdir=task_workdir)
                callback_telemetry.record("assertions_end", passed=verification.get("passed"))
                if success and verification["configured"] and not verification["passed"]:
                    success = False
                    reason = f"result assertions failed: {verification['failures']}"
                notes = answer.strip()[:200] or reason
        except Exception as exc:  # noqa: BLE001 — eval 不应因单任务崩溃中断
            success, reason = False, f"engine run failed: {exc}"
            notes = reason[:200]
            verification = evaluate_assertions(task, answer, workdir=task_workdir)
            callback_telemetry.record("task_exception", error=reason[:200])

        missing = [t for t in expected if t not in executed]
        return {
            "task_id": task["id"],
            "category": task["category"],
            "engine": self.engine_type,
            "success": success,
            "model": self.model,
            "token_count": estimate_tokens(task["prompt"]) + estimate_tokens(answer),
            "tool_calls": len(executed),
            "tool_failures": len(missing),
            "tools_used": executed,
            "successful_tools": successful_tools,
            "tool_events": tool_events,
            "notes": notes,
            "scoring": reason,
            "verification": verification,
            "turns_used": turns_used,
            "xenon_task_metrics": callback_telemetry.as_dict(),
            "execution_trace": {
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "events": callback_telemetry.events,
            },
        }

    @staticmethod
    def _score(
        task: dict[str, Any], executed: list[str], answer: str,
        *, successful_tools: list[str] | None = None,
        engine_type: str = "react",
    ) -> tuple[bool, str]:
        """评分：全部 expected_tools 成功执行且 final_answer 非空。"""
        expected = set(task.get("expected_tools", []))
        if engine_type in {"direct", "reflection"}:
            if not (answer or "").strip():
                return False, "empty final answer"
            return True, f"answer-only baseline ({engine_type}); tool expectation not applicable"
        observed = set(successful_tools if successful_tools is not None else executed)
        missing = expected - observed
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
            "Working directory: an isolated task fixture; use the relative target paths named above and do not prepend evals/fixtures or repository paths.",
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
        self.events: list[dict[str, Any]] = []

    def record(self, event: str, **fields: Any) -> None:
        self.events.append({
            "event": event,
            "at": datetime.now(timezone.utc).isoformat(),
            **fields,
        })

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
    engine_type: str = "react",
    max_iterations: int = 8,
    max_turns: int = 3,
    request_timeout: float = 30.0,
    checkpoint_path: str | Path | None = None,
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
        agent = RealAgent(
            model, engine_type=engine_type, workdir=workdir,
            isolate_tasks=isolate_tasks, max_iterations=max_iterations,
            max_turns=max_turns, request_timeout=request_timeout,
        )
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
        for task in tasks:
            result = agent.run_task(task)
            results.append(result)
            if checkpoint_path is not None:
                checkpoint = Path(checkpoint_path)
                checkpoint.parent.mkdir(parents=True, exist_ok=True)
                with checkpoint.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(result, ensure_ascii=False) + "\n")
                    stream.flush()
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


def run_repl_eval(tasks: list[dict[str, Any]], *, workdir: str | None = None) -> list[dict[str, Any]]:
    """Run the isolated slash-command suite through the real Xenon REPL."""
    agent = ReplCommandAgent(workdir=workdir)
    return [agent.run_task(task) for task in tasks]


def run_eval_matrix(
    tasks: list[dict[str, Any]],
    *,
    model: str,
    engines: list[str],
    workdir: str,
    isolate_tasks: bool = True,
    max_iterations: int = 4,
    max_turns: int = 1,
    request_timeout: float = 30.0,
) -> dict[str, list[dict[str, Any]]]:
    """Run the same task set independently through every selected engine."""
    unknown = sorted(set(engines) - set(SUPPORTED_ENGINE_TYPES))
    if unknown:
        raise ValueError(f"Unsupported engines: {unknown}")
    return {
        engine: run_eval(
            tasks, mode="real", model=model, engine_type=engine,
            workdir=workdir, isolate_tasks=isolate_tasks,
            max_iterations=max_iterations, max_turns=max_turns,
            request_timeout=request_timeout,
            checkpoint_path=Path(workdir) / "checkpoints" / f"{engine}.jsonl",
        )
        for engine in engines
    }


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
    tool_events = [
        event for result in results for event in result.get("tool_events", [])
        if isinstance(event, dict)
    ]
    tool_attempts = len(tool_events)
    tool_successes = sum(1 for event in tool_events if event.get("success") is True)
    configured_assertions = [
        result for result in results if result.get("verification", {}).get("configured")
    ]
    assertion_passes = sum(
        1 for result in configured_assertions
        if result.get("verification", {}).get("passed") is True
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
        "verified_success_rate": (verified_successes / total * 100) if total else 0.0,
        "tool_execution_attempts": tool_attempts,
        "tool_execution_successes": tool_successes,
        "tool_execution_success_rate": (tool_successes / tool_attempts * 100) if tool_attempts else None,
        "result_assertion_passes": assertion_passes,
        "result_assertion_pass_rate": (assertion_passes / len(configured_assertions) * 100)
        if configured_assertions else None,
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
    engine_name = str(results[0].get("engine", "n/a")) if results else "n/a"

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
        f"- Evaluation suite: `{'REPL command' if mode == 'repl' else 'ReAct/tool'}`",
        f"- Engine: `{engine_name}`",
        f"- Model: `{model}`",
        f"- Run date: `{date}`",
        f"- Tasks: {summary['tasks']}",
        f"- Success Rate: {summary['success_rate']:.1f}%",
        f"- Average Tokens: {summary['average_tokens']:.1f}",
        f"- Tool Calls: {summary['tool_calls']}",
        f"- Tool Failures: {summary['tool_failures']}",
        f"- Verified Tasks: {summary['verified_tasks']}/{summary['tasks']}",
        f"- Verified Success Rate: {_display_metric(summary['verification_rate'], suffix='%')}",
        f"- Verified Success Rate (all tasks): {_display_metric(summary['verified_success_rate'], suffix='%')}",
        f"- Tool Execution Success Rate: {_display_metric(summary['tool_execution_success_rate'], suffix='%')}",
        f"- Result Assertion Pass Rate: {_display_metric(summary['result_assertion_pass_rate'], suffix='%')}",
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


def write_matrix_report(
    matrix: dict[str, list[dict[str, Any]]],
    output_path: str | Path,
    *,
    model: str,
    run_date: str | None = None,
) -> Path:
    """Write one report per engine plus an index with non-aggregated scores."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    stem = output.stem
    engine_reports: dict[str, Path] = {}
    for engine, results in matrix.items():
        engine_path = output.with_name(f"{stem}-{engine}.md")
        write_report(results, engine_path, mode="real", model=model, run_date=run_date)
        engine_reports[engine] = engine_path

    lines = [
        "# Xenon Engine Matrix Report", "",
        f"- Model: `{model}`",
        f"- Engines: {', '.join(matrix)}",
        "",
        "> Each engine is scored independently. No cross-engine aggregate score is reported.",
        "",
        "| Engine | Verified Success | Tool Execution Success | Result Assertions | Report |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for engine, results in matrix.items():
        summary = summarize(results)
        report_name = engine_reports[engine].name
        lines.append(
            f"| `{engine}` | {_display_metric(summary['verified_success_rate'], suffix='%')} | "
            f"{_display_metric(summary['tool_execution_success_rate'], suffix='%')} | "
            f"{_display_metric(summary['result_assertion_pass_rate'], suffix='%')} | "
            f"[{report_name}]({report_name}) |"
        )
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
    parser.add_argument(
        "--suite", choices=["react", "repl"], default="react",
        help="react: model/tool evals; repl: real Xenon slash-command evals.",
    )
    parser.add_argument("--model", default=None, help="Required for --mode real, e.g. deepseek/deepseek-v4-pro")
    parser.add_argument(
        "--engines", default="react",
        help="Comma-separated engine types, or `all` for the full engine matrix.",
    )
    parser.add_argument("--max-iterations", type=int, default=8)
    parser.add_argument("--max-turns", type=int, default=3)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--tasks", default=None)
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
    if args.max_iterations < 1 or args.max_turns < 1 or args.request_timeout <= 0:
        parser.error("--max-iterations/--max-turns must be >= 1 and --request-timeout > 0")

    tasks_path = args.tasks or (DEFAULT_REPL_TASKS_PATH if args.suite == "repl" else DEFAULT_TASKS_PATH)
    tasks = load_tasks(tasks_path)
    if args.suite == "repl":
        if not args.workdir:
            parser.error("--workdir is required for --suite repl")
        with isolated_eval_credentials(args.workdir, args.credentials_path):
            results = run_repl_eval(tasks, workdir=args.workdir)
        report = write_report(results, args.output, mode="repl", model="xenon-repl")
        print(f"Wrote eval report: {report}")
        return 0
    if args.mode == "real" and not args.workdir:
        parser.error("--workdir is required for --mode real so file and credential writes stay isolated")
    if args.mode == "real":
        requested_engines = list(SUPPORTED_ENGINE_TYPES) if args.engines == "all" else [
            item.strip() for item in args.engines.split(",") if item.strip()
        ]
        unknown = sorted(set(requested_engines) - set(SUPPORTED_ENGINE_TYPES))
        if unknown:
            parser.error(f"unknown engine(s): {', '.join(unknown)}")
        if len(requested_engines) > 1:
            credentials_context = isolated_eval_credentials(args.workdir, args.credentials_path)
            with credentials_context:
                matrix = run_eval_matrix(
                    tasks, model=args.model, engines=requested_engines,
                    workdir=args.workdir, isolate_tasks=args.isolate_tasks,
                    max_iterations=args.max_iterations, max_turns=args.max_turns,
                    request_timeout=args.request_timeout,
                )
            report = write_matrix_report(matrix, args.output, model=args.model)
            print(f"Wrote engine matrix report: {report}")
            return 0
        engine_type = requested_engines[0]
    else:
        engine_type = "react"
    credentials_context = (
        isolated_eval_credentials(args.workdir, args.credentials_path)
        if args.mode == "real" and args.workdir
        else nullcontext()
    )
    with credentials_context:
        results = run_eval(
            tasks, mode=args.mode, model=args.model, engine_type=engine_type, workdir=args.workdir,
            isolate_tasks=args.isolate_tasks, max_iterations=args.max_iterations,
            max_turns=args.max_turns, request_timeout=args.request_timeout,
            checkpoint_path=(Path(args.workdir) / "checkpoints" / f"{engine_type}.jsonl")
            if args.mode == "real" and args.workdir else None,
        )
    model = args.model or "mock-agent"
    report = write_report(results, args.output, mode=args.mode, model=model)
    print(f"Wrote eval report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
