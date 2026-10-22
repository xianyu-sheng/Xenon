"""Small real-repository execution benchmark.

This is deliberately not a unit test: it calls a real model and writes to
throwaway git repositories.  The goal is to measure whether Xenon can finish
the full delivery loop (source change + target tests + commit + clean tree),
not just produce plausible text.

Usage:
    python evals/execution_benchmark.py --workdir D:/tmp/xenon-bench
    python evals/execution_benchmark.py --cases calc,fix_bug

The script returns non-zero when any case fails validation.  It is not part of
the offline test suite and does not run on CI.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from xenon.engine.callbacks import EngineCallback
from xenon.engine.context import AgentContext
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.task_runtime import run_with_delivery
from xenon.engine.task_state import TaskState
from xenon.repl.model_registry import ModelConfig


@dataclass
class Case:
    name: str
    objective: str
    verify_cmd: list[str]
    setup: Callable[[Path], None]
    extra_check: Callable[[Path], bool] | None = None


@dataclass
class CaseResult:
    name: str
    ok: bool
    reason: str
    elapsed: float
    tools: int
    failures: int
    task_status: str
    missing: list[str]
    commit: str
    test_returncode: int | None
    result_tail: str
    warnings: list[str]
    task_state: dict[str, Any]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _py_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "benchmark@example.com")
    _git(repo, "config", "user.name", "benchmark")
    (repo / "README.md").write_text("# benchmark\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "chore: benchmark baseline")


def _setup_calc(repo: Path) -> None:
    pass


def _setup_fix_bug(repo: Path) -> None:
    (repo / "math_utils.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    (repo / "test_math_utils.py").write_text(
        "from math_utils import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
        "    assert add(-1, 1) == 0\n",
        encoding="utf-8",
    )


def _setup_cli_summary(repo: Path) -> None:
    (repo / "app.py").write_text(
        "def coverage_summary(stats):\n    return ''\n", encoding="utf-8"
    )
    (repo / "test_app.py").write_text(
        "from app import coverage_summary\n\n"
        "def test_summary():\n"
        "    assert coverage_summary({'rules_total': 7, 'rules_full': 0}) == "
        "'Coverage: 7 rules ran, 0 declared full coverage'\n",
        encoding="utf-8",
    )


def _check_add(repo: Path) -> bool:
    result = subprocess.run(
        [sys.executable, "-c", "from math_utils import add; assert add(2,3)==5"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=_py_env(),
    )
    return result.returncode == 0


def _check_summary(repo: Path) -> bool:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app import coverage_summary; "
            "assert coverage_summary({'rules_total': 7, 'rules_full': 0}) "
            "== 'Coverage: 7 rules ran, 0 declared full coverage'",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=_py_env(),
    )
    return result.returncode == 0


CASES = {
    "calc": Case(
        name="calc",
        objective=(
            "在当前仓库完成最小功能：新增 calc.py，定义 add(a, b) 返回两数之和；"
            "新增 tests/test_calc.py，至少测试 2 + 3 == 5 和 -1 + 1 == 0；"
            "运行 python -m pytest tests/test_calc.py -q 直到真实通过；"
            "然后 git add calc.py tests/test_calc.py && "
            "git commit -m 'feat: add calc'；不要 push，不要创建 PR。"
        ),
        verify_cmd=[sys.executable, "-m", "pytest", "tests/test_calc.py", "-q"],
        setup=_setup_calc,
    ),
    "fix_bug": Case(
        name="fix_bug",
        objective=(
            "当前仓库的 math_utils.add 返回 a-b，但 test_math_utils.py 期望 a+b。"
            "请修复源码使测试真实通过，运行 python -m pytest test_math_utils.py -q；"
            "然后 git add math_utils.py && git commit -m 'fix: add returns sum'；"
            "不要改测试来绕过，不要 push，不要创建 PR。"
        ),
        verify_cmd=[sys.executable, "-m", "pytest", "test_math_utils.py", "-q"],
        setup=_setup_fix_bug,
        extra_check=_check_add,
    ),
    "cli_summary": Case(
        name="cli_summary",
        objective=(
            "当前仓库的 app.coverage_summary 返回空字符串。请实现它，使 "
            "coverage_summary({'rules_total': 7, 'rules_full': 0}) 返回 "
            "'Coverage: 7 rules ran, 0 declared full coverage'，数字不得写死；"
            "运行 python -m pytest test_app.py -q；然后 "
            "git add app.py && git commit -m 'feat: add coverage summary'；"
            "不要 push，不要创建 PR。"
        ),
        verify_cmd=[sys.executable, "-m", "pytest", "test_app.py", "-q"],
        setup=_setup_cli_summary,
        extra_check=_check_summary,
    ),
}


class _Trace(EngineCallback):
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def on_warning(self, warning: str) -> None:
        self.warnings.append(str(warning))


def _load_model(model: str, base_url: str) -> tuple[ModelConfig, dict[str, Any]]:
    credentials_path = Path.home() / ".xenon" / "credentials.yaml"
    credentials = yaml.safe_load(credentials_path.read_text(encoding="utf-8"))
    provider = model.split("/", 1)[0]
    api_key = credentials[provider]
    config = ModelConfig(
        model_id=model,
        alias=model.split("/")[-1],
        api_key=api_key,
        base_url=base_url,
        max_tokens=8192,
        context_window=128000,
    )
    return config, {"model": model, "base_url": base_url}


def _run_case(
    case: Case,
    workdir: Path,
    config: ModelConfig,
    *,
    model: str,
) -> CaseResult:
    repo = workdir / case.name
    if repo.exists():
        shutil.rmtree(repo, ignore_errors=True)
    if repo.exists():
        # Windows can keep .git object handles briefly after a previous run.
        repo = workdir / f"{case.name}-{int(time.time())}"
    repo.mkdir(parents=True)
    _init_repo(repo)
    case.setup(repo)
    if case.setup is not _setup_calc:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "chore: seed benchmark case")
    baseline = _git(repo, "rev-parse", "HEAD").stdout.strip()
    os.chdir(repo)

    trace = _Trace()
    engine = PlanExecuteEngine(
        [model],
        max_steps=0,
        max_replans=2,
        native_planning=True,
        plan_max_tokens=8192,
        model_configs={model: config},
        callback=trace,
    )
    state = TaskState(
        objective=case.objective,
        intent="write_code",
        execution_level=3,
        required=["verify", "commit"],
    )
    ctx = AgentContext(
        {
            "_session_id": "benchmark-" + case.name,
            "_execution_level": 3,
            "_workspace_root": str(repo),
            "_task_state": state.to_dict(),
        }
    )
    started = time.time()
    error = ""
    result_text = ""
    try:
        result_text = str(
            run_with_delivery(engine, case.objective, ctx, max_delivery_retries=3)
        )
    except Exception:  # noqa: BLE001 — report the failure, keep the repo
        error = traceback.format_exc()
    elapsed = round(time.time() - started, 1)

    final_state = TaskState.from_context(ctx) or state
    missing = final_state.missing_delivery()
    tracker = getattr(engine, "_last_tracker", None)
    calls = list(getattr(tracker, "calls", []) or [])
    test_returncode: int | None = None
    reason = ""
    if error:
        reason = error.splitlines()[-1][:500]
    else:
        test = subprocess.run(
            case.verify_cmd,
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=_py_env(),
        )
        test_returncode = test.returncode
        status = _git(repo, "status", "--porcelain").stdout.strip()
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        if test.returncode != 0:
            reason = "target tests failed: " + (test.stdout + test.stderr)[-800:]
        elif status:
            reason = "worktree dirty: " + status[:500]
        elif head == baseline:
            reason = "no new commit"
        elif missing:
            reason = "TaskState missing: " + ", ".join(missing)
        elif final_state.status != "completed":
            reason = "TaskState status: " + final_state.status
        elif case.extra_check is not None and not case.extra_check(repo):
            reason = "case-specific source check failed"

    return CaseResult(
        name=case.name,
        ok=not reason,
        reason=reason,
        elapsed=elapsed,
        tools=len(calls),
        failures=sum(1 for call in calls if not call.success),
        task_status=final_state.status,
        missing=missing,
        commit=final_state.commit,
        test_returncode=test_returncode,
        result_tail=result_text[-500:],
        warnings=trace.warnings[-10:],
        task_state=final_state.to_dict(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workdir",
        default=str(Path(os.environ.get("TEMP", "/tmp")) / "xenon-bench"),
    )
    parser.add_argument("--cases", default=",".join(CASES))
    parser.add_argument("--model", default="deepseek/deepseek-v4-flash")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)

    names = [name.strip() for name in args.cases.split(",") if name.strip()]
    unknown = [name for name in names if name not in CASES]
    if unknown:
        parser.error("unknown cases: " + ", ".join(unknown))

    config, _meta = _load_model(args.model, args.base_url)
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    results: list[CaseResult] = []
    for name in names:
        print(f"\n=== {name} ===", flush=True)
        result = _run_case(CASES[name], workdir, config, model=args.model)
        results.append(result)
        icon = "PASS" if result.ok else "FAIL"
        print(
            f"{icon} {name}: {result.reason or 'ok'} "
            f"({result.elapsed}s, {result.tools} tools, "
            f"{result.failures} failures)",
            flush=True,
        )
        if not result.ok:
            print(result.result_tail, flush=True)

    passed = sum(1 for result in results if result.ok)
    print(f"\nRESULT {passed}/{len(results)} cases passed")
    payload = {
        "model": args.model,
        "passed": passed,
        "total": len(results),
        "cases": [vars(result) for result in results],
    }
    output = Path(args.json) if args.json else workdir / "benchmark.json"
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"JSON: {output}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
