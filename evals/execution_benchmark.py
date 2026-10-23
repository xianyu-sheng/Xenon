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


def _setup_json_report(repo: Path) -> None:
    (repo / "report.py").write_text("def build_report(stats):\n    return {'stats': dict(stats)}\n", encoding="utf-8")
    (repo / "test_report.py").write_text("from report import build_report\n\ndef test_report_fields():\n    data = build_report({'rules_total': 7, 'rules_full': 0})\n    assert data['rules_total'] == 7\n    assert data['rules_full'] == 0\n", encoding="utf-8")


def _setup_multi_file(repo: Path) -> None:
    package = repo / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "calc.py").write_text('def add(a, b):\n    return a - b\n', encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text('from pkg.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n', encoding="utf-8")


def _setup_rename_function(repo: Path) -> None:
    (repo / "utils.py").write_text('def add(a, b):\n    return a + b\n', encoding="utf-8")
    (repo / "test_utils.py").write_text('from utils import sum_two\n\ndef test_sum_two():\n    assert sum_two(2, 3) == 5\n', encoding="utf-8")


def _setup_two_bugs(repo: Path) -> None:
    (repo / "ops.py").write_text('def add(a, b):\n    return a - b\n\ndef mul(a, b):\n    return a + b\n', encoding="utf-8")
    (repo / "test_ops.py").write_text('from ops import add, mul\n\ndef test_ops():\n    assert add(2, 3) == 5\n    assert mul(2, 3) == 6\n', encoding="utf-8")


def _setup_format_price(repo: Path) -> None:
    (repo / "pricing.py").write_text('def format_price(cents):\n    return str(cents)\n', encoding="utf-8")
    (repo / "test_pricing.py").write_text("from pricing import format_price\n\ndef test_price():\n    assert format_price(123) == '$1.23'\n    assert format_price(5) == '$0.05'\n", encoding="utf-8")


def _setup_safe_div(repo: Path) -> None:
    (repo / "mathx.py").write_text('def safe_div(a, b):\n    return a / b\n', encoding="utf-8")
    (repo / "test_mathx.py").write_text('from mathx import safe_div\n\ndef test_div():\n    assert safe_div(4, 2) == 2\n    assert safe_div(1, 0) is None\n', encoding="utf-8")


def _setup_config_default(repo: Path) -> None:
    (repo / "config.py").write_text("def enabled(cfg):\n    return cfg.get('enabled')\n", encoding="utf-8")
    (repo / "test_config.py").write_text("from config import enabled\n\ndef test_default():\n    assert enabled({}) is False\n    assert enabled({'enabled': True}) is True\n", encoding="utf-8")


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


def _check_json_fields(repo: Path) -> bool:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from report import build_report; "
            "d=build_report({'rules_total': 7, 'rules_full': 0}); "
            "assert d['rules_total'] == 7 and d['rules_full'] == 0",
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


def _check_package_add(repo: Path) -> bool:
    result = subprocess.run(
        [sys.executable, "-c", "from pkg.calc import add; assert add(2,3)==5"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=_py_env(),
    )
    return result.returncode == 0


def _check_rename(repo: Path) -> bool:
    return subprocess.run(
        [sys.executable, "-c", "from utils import sum_two; assert sum_two(2,3)==5"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False, env=_py_env(),
    ).returncode == 0


def _check_two_bugs(repo: Path) -> bool:
    return subprocess.run(
        [sys.executable, "-c", "from ops import add,mul; assert add(2,3)==5 and mul(2,3)==6"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False, env=_py_env(),
    ).returncode == 0


def _check_price(repo: Path) -> bool:
    return subprocess.run(
        [sys.executable, "-c", "from pricing import format_price; assert format_price(123)=='$1.23' and format_price(5)=='$0.05'"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False, env=_py_env(),
    ).returncode == 0


def _check_div(repo: Path) -> bool:
    return subprocess.run(
        [sys.executable, "-c", "from mathx import safe_div; assert safe_div(4,2)==2 and safe_div(1,0) is None"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False, env=_py_env(),
    ).returncode == 0


def _check_config(repo: Path) -> bool:
    return subprocess.run(
        [sys.executable, "-c", "from config import enabled; assert enabled({}) is False and enabled({'enabled': True}) is True"],
        cwd=repo, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False, env=_py_env(),
    ).returncode == 0


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
    "json_report": Case(
        name="json_report",
        objective=(
            "当前仓库的 report.build_report 只把 stats 放在嵌套的 stats 字段里。请让 "
            "build_report({'rules_total': 7, 'rules_full': 0}) 返回的字典同时包含"
            "顶层 rules_total=7 和 rules_full=0，数字来自输入、不得写死；运行 "
            "python -m pytest test_report.py -q；然后 git add report.py && "
            "git commit -m 'feat: expose report coverage fields'；不要 push，不要创建 PR。"
        ),
        verify_cmd=[sys.executable, "-m", "pytest", "test_report.py", "-q"],
        setup=_setup_json_report,
        extra_check=_check_json_fields,
    ),
    "multi_file": Case(
        name="multi_file",
        objective=(
            "当前仓库 pkg/calc.py 的 add 返回 a-b，tests/test_calc.py 期望 a+b。请修复"
            "包内源码使测试真实通过，运行 python -m pytest tests/test_calc.py -q；"
            "然后 git add pkg/calc.py && git commit -m 'fix: package add returns sum'；"
            "不要改测试来绕过，不要 push，不要创建 PR。"
        ),
        verify_cmd=[sys.executable, "-m", "pytest", "tests/test_calc.py", "-q"],
        setup=_setup_multi_file,
        extra_check=_check_package_add,
    ),
    "rename_function": Case(
        name="rename_function",
        objective=(
            "当前仓库 utils.py 只有 add 函数，tests/test_utils.py 期望导入 sum_two。"
            "请新增 sum_two(a, b) 返回 a+b，并保留 add 兼容；运行 "
            "python -m pytest test_utils.py -q；然后 git add utils.py && "
            "git commit -m 'feat: add sum_two alias'；不要 push，不要创建 PR。"
        ),
        verify_cmd=[sys.executable, "-m", "pytest", "test_utils.py", "-q"],
        setup=_setup_rename_function,
        extra_check=_check_rename,
    ),
    "two_bugs": Case(
        name="two_bugs",
        objective=(
            "ops.py 的 add 和 mul 都返回了错误结果，test_ops.py 描述正确行为。"
            "请修复两个函数并运行 python -m pytest test_ops.py -q；然后 "
            "git add ops.py && git commit -m 'fix: correct arithmetic ops'；"
            "不要修改测试，不要 push，不要创建 PR。"
        ),
        verify_cmd=[sys.executable, "-m", "pytest", "test_ops.py", "-q"],
        setup=_setup_two_bugs,
        extra_check=_check_two_bugs,
    ),
    "format_price": Case(
        name="format_price",
        objective=(
            "pricing.format_price 目前直接返回 cents 的字符串，test_pricing.py 期望 "
            "123 -> '$1.23'、5 -> '$0.05'。请实现两位小数和美元符号，运行 "
            "python -m pytest test_pricing.py -q；然后 git add pricing.py && "
            "git commit -m 'feat: format prices'；不要 push，不要创建 PR。"
        ),
        verify_cmd=[sys.executable, "-m", "pytest", "test_pricing.py", "-q"],
        setup=_setup_format_price,
        extra_check=_check_price,
    ),
    "safe_div": Case(
        name="safe_div",
        objective=(
            "mathx.safe_div 在除数为 0 时抛异常，test_mathx.py 期望返回 None。"
            "请修复边界行为，保留正常除法；运行 python -m pytest test_mathx.py -q；"
            "然后 git add mathx.py && git commit -m 'fix: safe division by zero'；"
            "不要修改测试，不要 push，不要创建 PR。"
        ),
        verify_cmd=[sys.executable, "-m", "pytest", "test_mathx.py", "-q"],
        setup=_setup_safe_div,
        extra_check=_check_div,
    ),
    "config_default": Case(
        name="config_default",
        objective=(
            "config.enabled 对缺失字段返回 None，test_config.py 期望默认 False。"
            "请实现默认值，运行 python -m pytest test_config.py -q；然后 "
            "git add config.py && git commit -m 'fix: enabled defaults to false'；"
            "不要修改测试，不要 push，不要创建 PR。"
        ),
        verify_cmd=[sys.executable, "-m", "pytest", "test_config.py", "-q"],
        setup=_setup_config_default,
        extra_check=_check_config,
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
        status_lines = [
            line
            for line in _git(repo, "status", "--porcelain", "-uall").stdout.splitlines()
            if line.strip()
            and "__pycache__/" not in line
            and ".pytest_cache/" not in line
            and not line.rstrip().endswith(".pyc")
        ]
        status = chr(10).join(status_lines).strip()
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
    parser.add_argument("--cases", default="calc,fix_bug,cli_summary,json_report,multi_file,rename_function,two_bugs,format_price,safe_div,config_default")
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
