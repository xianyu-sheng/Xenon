"""C16: harder, pre-registered agentic-completion suite (v2).

The v1 suite was ceiling-limited (flash and pro both 5/5).  v2 keeps the same
deterministic scoring but adds hidden edge cases, cross-file API changes,
multiple bugs, a git history task, and a longer implementation chain.

Run:
    python -m evals.agentic_completion_v2 --models flash pro
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from evals.agentic_completion import (
    MODEL_ALIASES,
    RunResult,
    Task,
    _load_credentials,
    _pytest,
    _run_task,
    _write,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "evals" / "reports" / "agentic_completion_v2.json"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── 1. hidden edge cases ────────────────────────────────────────────────


def _setup_hidden_edges(work: Path) -> dict[str, Any]:
    _write(
        work / "calc.py",
        'def safe_divide(a, b):\n'
        '    """Return a / b, or None when b == 0."""\n'
        "    return a / b\n",
    )
    visible = (
        "from calc import safe_divide\n\n"
        "def test_normal():\n"
        "    assert safe_divide(6, 3) == 2\n"
    )
    _write(work / "test_calc.py", visible)
    return {"visible_hash": _sha(visible)}


def _check_hidden_edges(work: Path, meta: dict[str, Any]) -> tuple[bool, str]:
    if _sha((work / "test_calc.py").read_text(encoding="utf-8")) != meta["visible_hash"]:
        return False, "visible test was modified"
    _write(
        work / "test_hidden_calc.py",
        "from calc import safe_divide\n\n"
        "def test_zero_denominator():\n"
        "    assert safe_divide(1, 0) is None\n\n"
        "def test_negative():\n"
        "    assert safe_divide(-4, 2) == -2\n\n"
        "def test_zero_numerator():\n"
        "    assert safe_divide(0, 5) == 0\n",
    )
    return _pytest(work)


# ── 2. cross-file API change ───────────────────────────────────────────


def _setup_cross_file(work: Path) -> dict[str, Any]:
    _write(work / "shop/__init__.py", "")
    _write(
        work / "shop/pricing.py",
        "def calculate_total(items):\n    return sum(items)\n",
    )
    _write(
        work / "shop/cart.py",
        "from .pricing import calculate_total\n\n"
        "def cart_total(items):\n    return calculate_total(items)\n",
    )
    _write(
        work / "shop/invoice.py",
        "from .pricing import calculate_total\n\n"
        "def invoice_total(items):\n    return calculate_total(items)\n",
    )
    _write(
        work / "test_shop.py",
        "from shop.cart import cart_total\n"
        "from shop.invoice import invoice_total\n\n"
        "def test_cart():\n    assert cart_total([1, 2]) == 3\n\n"
        "def test_invoice():\n    assert invoice_total([1, 2]) == 3\n",
    )
    return {}


def _check_cross_file(work: Path, meta: dict[str, Any]) -> tuple[bool, str]:
    _write(
        work / "test_hidden_tax.py",
        "from shop.pricing import calculate_total\n"
        "from shop.cart import cart_total\n"
        "from shop.invoice import invoice_total\n\n"
        "import pytest\n\n"
        "def test_pricing_tax():\n"
        "    assert calculate_total([100], 0.1) == pytest.approx(110)\n\n"
        "def test_cart_tax():\n"
        "    assert cart_total([100], 0.1) == pytest.approx(110)\n\n"
        "def test_invoice_tax():\n"
        "    assert invoice_total([100], 0.1) == pytest.approx(110)\n",
    )
    return _pytest(work)


# ── 3. multiple bugs ───────────────────────────────────────────────────


def _setup_multi_bug(work: Path) -> dict[str, Any]:
    _write(
        work / "utils.py",
        "def normalize(name):\n"
        "    return name.lower()\n\n"
        "def slugify(name):\n"
        "    return normalize(name).replace(' ', '-')\n",
    )
    _write(
        work / "parser.py",
        "def parse_pair(text):\n"
        "    key, value = text.split('=')\n"
        "    return key, value\n",
    )
    _write(
        work / "test_utils.py",
        "from utils import normalize, slugify\n\n"
        "def test_normalize():\n    assert normalize(' Foo ') == 'foo'\n\n"
        "def test_slugify():\n    assert slugify(' Foo Bar ') == 'foo-bar'\n",
    )
    _write(
        work / "test_parser.py",
        "from parser import parse_pair\n\n"
        "def test_parse():\n    assert parse_pair('a=1') == ('a', '1')\n",
    )
    return {}


def _check_multi_bug(work: Path, meta: dict[str, Any]) -> tuple[bool, str]:
    _write(
        work / "test_hidden_edges.py",
        "from utils import normalize, slugify\n"
        "from parser import parse_pair\n\n"
        "def test_normalize_strip():\n"
        "    assert normalize('  Foo  ') == 'foo'\n\n"
        "def test_slugify_strip():\n"
        "    assert slugify('  Foo Bar  ') == 'foo-bar'\n\n"
        "def test_missing_equals():\n"
        "    assert parse_pair('bad') == (None, None)\n",
    )
    return _pytest(work)


# ── 4. git history: revert only the bad part ───────────────────────────


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


def _setup_git_revert(work: Path) -> dict[str, Any]:
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.email", "agent@example.com"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "Agent"], cwd=work, check=True)
    _write(work / "config.py", "TIMEOUT = 30\n")
    subprocess.run(["git", "add", "config.py"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "chore: initial config"], cwd=work, check=True)
    _write(work / "config.py", "TIMEOUT = 5\n")
    _write(work / "feature.py", "FEATURE_ENABLED = True\n")
    subprocess.run(["git", "add", "config.py", "feature.py"], cwd=work, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "feat: add feature (and bad timeout)"],
        cwd=work,
        check=True,
    )
    return {}


def _check_git_revert(work: Path, meta: dict[str, Any]) -> tuple[bool, str]:
    if "TIMEOUT = 30" not in (work / "config.py").read_text(encoding="utf-8"):
        return False, "TIMEOUT was not restored"
    if not (work / "feature.py").exists():
        return False, "feature.py was removed"
    branch = _git(work, "branch", "--show-current")
    if branch != "fix/timeout":
        return False, f"branch={branch!r}"
    log = _git(work, "log", "-1", "--format=%s")
    if "fix" not in log.lower():
        return False, f"last commit={log!r}"
    if _git(work, "status", "--porcelain"):
        return False, "worktree not clean"
    return True, f"branch={branch}; commit={log}"


# ── 5. longer implementation chain ─────────────────────────────────────


def _setup_stats(work: Path) -> dict[str, Any]:
    _write(
        work / "stats.py",
        'def mean(values):\n'
        '    """Return arithmetic mean; None for an empty list."""\n'
        "    raise NotImplementedError\n\n"
        'def median(values):\n'
        '    """Return median; None for an empty list."""\n'
        "    raise NotImplementedError\n\n"
        'def mode(values):\n'
        '    """Return the most common value; ties pick the smallest; None if empty."""\n'
        "    raise NotImplementedError\n",
    )
    _write(
        work / "test_stats.py",
        "from stats import mean\n\n"
        "def test_mean():\n    assert mean([1, 2, 3]) == 2\n",
    )
    return {}


def _check_stats(work: Path, meta: dict[str, Any]) -> tuple[bool, str]:
    _write(
        work / "test_hidden_stats.py",
        "from stats import mean, median, mode\n\n"
        "def test_empty():\n"
        "    assert mean([]) is None\n"
        "    assert median([]) is None\n"
        "    assert mode([]) is None\n\n"
        "def test_median_even():\n"
        "    assert median([1, 2, 3, 4]) == 2.5\n\n"
        "def test_mode_tie():\n"
        "    assert mode([2, 2, 1, 1]) == 1\n",
    )
    return _pytest(work)


TASKS: list[Task] = [
    Task(
        name="hidden_edge_cases",
        prompt=(
            "calc.py has a bug in safe_divide. Make it match its docstring, "
            "do not modify test_calc.py, and run pytest. Hidden tests will "
            "check b == 0 and negative values."
        ),
        setup=_setup_hidden_edges,
        check=_check_hidden_edges,
        max_iterations=24,
    ),
    Task(
        name="cross_file_api_change",
        prompt=(
            "Change shop/pricing.py calculate_total(items) to "
            "calculate_total(items, tax_rate=0.0), returning "
            "sum(items) * (1 + tax_rate). Update cart_total and invoice_total "
            "to accept tax_rate=0.0 and pass it through. Update/add tests and "
            "run pytest."
        ),
        setup=_setup_cross_file,
        check=_check_cross_file,
        max_iterations=36,
    ),
    Task(
        name="multi_bug_diagnosis",
        prompt=(
            "Run pytest, diagnose all failures, and fix the code (not the tests). "
            "normalize must strip surrounding whitespace; parse_pair must return "
            "(None, None) when '=' is absent. Hidden tests check edge cases."
        ),
        setup=_setup_multi_bug,
        check=_check_multi_bug,
        max_iterations=30,
    ),
    Task(
        name="git_partial_revert",
        prompt=(
            "HEAD is a bad commit: it changed TIMEOUT from 30 to 5, but the new "
            "feature.py is wanted. Restore TIMEOUT=30 only, keep feature.py, "
            "create branch fix/timeout, and commit with a fix message. Do not push."
        ),
        setup=_setup_git_revert,
        check=_check_git_revert,
        max_iterations=30,
    ),
    Task(
        name="stats_chain",
        prompt=(
            "Implement mean, median and mode in stats.py according to their "
            "docstrings. Add pytest tests for median/mode/empty, then run pytest. "
            "Hidden tests check even-length median, ties, and empty input."
        ),
        setup=_setup_stats,
        check=_check_stats,
        max_iterations=30,
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models",
        nargs="+",
        default=["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"],
    )
    parser.add_argument("--tasks", nargs="*", default=None)
    parser.add_argument("--max-iterations", type=int, default=36)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    args.models = [MODEL_ALIASES.get(model, model) for model in args.models]
    os.environ.setdefault("XENON_ENGINE_TIMEOUT", "600")
    credentials = _load_credentials()
    selected = [task for task in TASKS if not args.tasks or task.name in args.tasks]
    work_root = Path(tempfile.mkdtemp(prefix="xenon-agentic-v2-runs-"))
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
