"""First agentic-completion benchmark for Xenon.

Pre-registered before the first run: the five tasks, their setup, prompts and
pass checks are fixed in this file.  The benchmark measures whether the engine
actually completes a multi-step coding task (files exist, tests pass, commit
exists) rather than whether the final prose sounds plausible.

Run:
    python -m evals.agentic_completion --models flash pro

Results are printed and written to ``evals/reports/agentic_completion.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from xenon.engine.callbacks import EngineCallback
from xenon.engine.react_engine import ReActEngine
from xenon.repl.model_registry import ModelConfig

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "evals" / "reports"
DEFAULT_OUTPUT = REPORT_DIR / "agentic_completion.json"
MODEL_ALIASES = {
    "flash": "deepseek/deepseek-v4-flash",
    "pro": "deepseek/deepseek-v4-pro",
}


@dataclass
class Task:
    name: str
    prompt: str
    setup: Callable[[Path], dict[str, Any]] | None = None
    check: Callable[[Path, dict[str, Any]], tuple[bool, str]] | None = None
    max_iterations: int = 30


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _pytest(work: Path) -> tuple[bool, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=work,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env=env,
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
    return proc.returncode == 0, " | ".join(tail)


def _check_file_set(work: Path, files: list[str]) -> tuple[bool, str]:
    missing = [name for name in files if not (work / name).exists()]
    if missing:
        return False, f"missing files: {missing}"
    return True, "files present"


def _setup_fix_bug(work: Path) -> dict[str, Any]:
    _write(work / "calc.py", "def add(a, b):\n    return a - b\n")
    test = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    _write(work / "test_calc.py", test)
    return {"test_hash": __import__("hashlib").sha256(test.encode()).hexdigest()}


def _check_fix_bug(work: Path, meta: dict[str, Any]) -> tuple[bool, str]:
    import hashlib

    test = (work / "test_calc.py").read_text(encoding="utf-8")
    if hashlib.sha256(test.encode()).hexdigest() != meta["test_hash"]:
        return False, "test file was modified"
    ok, detail = _pytest(work)
    return ok, detail


def _setup_refactor(work: Path) -> dict[str, Any]:
    _write(
        work / "lib.py",
        "def compute_total(items):\n    return sum(items)\n",
    )
    _write(
        work / "app.py",
        "from lib import compute_total\n\nprint(compute_total([1, 2]))\n",
    )
    _write(
        work / "test_app.py",
        "from lib import compute_total\n\n"
        "def test_total():\n    assert compute_total([1, 2]) == 3\n",
    )
    return {}


def _check_refactor(work: Path, meta: dict[str, Any]) -> tuple[bool, str]:
    ok, detail = _pytest(work)
    if not ok:
        return False, detail
    offenders = [
        str(p.relative_to(work))
        for p in work.rglob("*.py")
        if "compute_total" in p.read_text(encoding="utf-8")
    ]
    if offenders:
        return False, f"old name remains in: {offenders}"
    return True, detail


def _setup_git(work: Path) -> dict[str, Any]:
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.email", "agent@example.com"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "Agent"], cwd=work, check=True)
    _write(work / "README.md", "# agentic task\n")
    subprocess.run(["git", "add", "README.md"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "chore: seed"], cwd=work, check=True)
    return {}


def _git(work: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=work,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return (proc.stdout + proc.stderr).strip()


def _check_git(work: Path, meta: dict[str, Any]) -> tuple[bool, str]:
    notes = work / "notes.txt"
    if not notes.exists() or notes.read_text(encoding="utf-8").strip() != "agentic-ok":
        return False, "notes.txt missing/wrong"
    branch = _git(work, "branch", "--show-current")
    if branch != "feat/agentic":
        return False, f"branch={branch!r}"
    log = _git(work, "log", "-1", "--format=%s")
    if "feat" not in log.lower():
        return False, f"last commit={log!r}"
    if _git(work, "status", "--porcelain"):
        return False, "worktree not clean"
    return True, f"branch={branch}; commit={log}"


TASKS: list[Task] = [
    Task(
        name="create_file_and_test",
        prompt=(
            "In the current directory, create greet.py with a function "
            "greet(name) returning 'Hello, <name>!'. Create test_greet.py "
            "with pytest tests for it. Run pytest and make it pass. "
            "Write the files to disk; do not just print code."
        ),
        check=lambda work, meta: (
            _check_file_set(work, ["greet.py", "test_greet.py"])
            if not (work / "greet.py").exists() or not (work / "test_greet.py").exists()
            else _pytest(work)
        ),
    ),
    Task(
        name="fix_existing_bug",
        prompt=(
            "calc.py has a bug. Fix calc.py so that pytest passes. "
            "Do not modify test_calc.py."
        ),
        setup=_setup_fix_bug,
        check=_check_fix_bug,
    ),
    Task(
        name="multi_file_package",
        prompt=(
            "Create a package mathx with mathx/__init__.py and mathx/ops.py. "
            "ops.py must define add(a, b) and subtract(a, b); __init__.py must "
            "export both. Add test_mathx.py with pytest tests for both. "
            "Run pytest and make it pass."
        ),
        check=lambda work, meta: (
            _check_file_set(
                work, ["mathx/__init__.py", "mathx/ops.py", "test_mathx.py"]
            )
            if not all(
                (work / name).exists()
                for name in ("mathx/__init__.py", "mathx/ops.py", "test_mathx.py")
            )
            else _pytest(work)
        ),
    ),
    Task(
        name="refactor_rename",
        prompt=(
            "Rename compute_total to total_price across all Python files, "
            "update all imports/calls, then run pytest and make it pass."
        ),
        setup=_setup_refactor,
        check=_check_refactor,
    ),
    Task(
        name="git_branch_commit",
        prompt=(
            "Create notes.txt containing exactly 'agentic-ok'. Create a branch "
            "feat/agentic, commit the change with a feat message, and do not push."
        ),
        setup=_setup_git,
        check=_check_git,
        max_iterations=24,
    ),
]


class Trace(EngineCallback):
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.loop_hints = 0
        self.llm_calls = 0

    def on_warning(self, message: str) -> None:
        self.warnings.append(message)
        if "潜在循环" in message:
            self.loop_hints += 1


@dataclass
class RunResult:
    model: str
    task: str
    passed: bool
    detail: str
    elapsed_s: float
    llm_calls: int
    tool_calls: int
    failed_tools: int
    loop_hints: int
    warnings: list[str] = field(default_factory=list)


def _load_credentials() -> dict[str, Any]:
    path = Path.home() / ".xenon" / "credentials.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _run_task(
    model: str,
    task: Task,
    work_root: Path,
    credentials: dict[str, Any],
    max_iterations: int,
) -> RunResult:
    work = Path(tempfile.mkdtemp(prefix=f"xenon-agentic-{task.name}-", dir=work_root))
    meta = task.setup(work) if task.setup else {}
    provider = model.split("/", 1)[0].lower()
    config = ModelConfig(
        model_id=model,
        alias=model.rsplit("/", 1)[-1],
        api_key=credentials.get(provider, ""),
        base_url="https://api.deepseek.com" if provider == "deepseek" else None,
        max_tokens=4096,
        context_window=128000,
    )
    trace = Trace()
    previous = Path.cwd()
    os.chdir(work)
    started = time.monotonic()
    tracker = None
    try:
        engine = ReActEngine(
            [model],
            max_iterations=task.max_iterations or max_iterations,
            native_fc=True,
            model_configs={model: config},
            callback=trace,
        )
        for attr in ("_call_llm_native_for_phase", "_call_llm_for_phase"):
            original = getattr(engine, attr)

            def make_spy(fn):
                def spy(*args, **kwargs):
                    trace.llm_calls += 1
                    return fn(*args, **kwargs)

                return spy

            setattr(engine, attr, make_spy(original))
        engine.run(task.prompt)
        tracker = engine._last_tracker
    except Exception:  # noqa: BLE001 - benchmark records failures via checks
        pass
    finally:
        os.chdir(previous)

    if task.check:
        passed, detail = task.check(work, meta)
    else:
        passed, detail = False, "task has no check"
    calls = list(tracker.calls) if tracker else []
    return RunResult(
        model=model,
        task=task.name,
        passed=passed,
        detail=detail,
        elapsed_s=round(time.monotonic() - started, 1),
        llm_calls=trace.llm_calls,
        tool_calls=len(calls),
        failed_tools=sum(1 for call in calls if not call.success),
        loop_hints=trace.loop_hints,
        warnings=trace.warnings,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models",
        nargs="+",
        default=["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"],
    )
    parser.add_argument("--tasks", nargs="*", default=None)
    parser.add_argument("--max-iterations", type=int, default=30)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    args.models = [MODEL_ALIASES.get(model, model) for model in args.models]
    os.environ.setdefault("XENON_ENGINE_TIMEOUT", "600")
    credentials = _load_credentials()
    selected = [task for task in TASKS if not args.tasks or task.name in args.tasks]
    work_root = Path(tempfile.mkdtemp(prefix="xenon-agentic-runs-"))
    print(f"work root: {work_root}")
    results: list[RunResult] = []
    for model in args.models:
        for task in selected:
            print(f"\n=== {model} :: {task.name} ===", flush=True)
            result = _run_task(
                model, task, work_root, credentials, args.max_iterations
            )
            results.append(result)
            print(
                f"{'PASS' if result.passed else 'FAIL'} | {result.detail} | "
                f"{result.elapsed_s}s | llm={result.llm_calls} "
                f"tools={result.tool_calls} fail={result.failed_tools} "
                f"hints={result.loop_hints}",
                flush=True,
            )

    print("\n=== SUMMARY ===")
    for model in args.models:
        rows = [row for row in results if row.model == model]
        passed = sum(1 for row in rows if row.passed)
        print(
            f"{model}: {passed}/{len(rows)} passed | "
            f"llm={sum(r.llm_calls for r in rows)} "
            f"tools={sum(r.tool_calls for r in rows)} "
            f"failed={sum(r.failed_tools for r in rows)} "
            f"time={sum(r.elapsed_s for r in rows):.1f}s"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps([row.__dict__ for row in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nJSON: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
