"""Real end-to-end smoke: drive Xenon the way a user does.

Unit tests only prove the pieces agree with each other.  This script runs the
*shipped entry points* against a throwaway git repo with a real model, then
checks what a user would check: did the file change, did the test pass, was
there a commit, what does `xenon metrics` say about it.

Every scenario records its raw artifacts under ``--out`` so a failure can be
diagnosed without re-running (re-running costs tokens).

Usage::

    python scripts/e2e_smoke.py --model deepseek/deepseek-v4-flash
    python scripts/e2e_smoke.py --only edit-commit,plan-execute

Not part of CI: it needs a real provider and spends tokens.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PASS = "PASS"
FAIL = "FAIL"

CALC = "def add(a, b):\n    return a + b\n"
TEST_CALC = (
    "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
)


@dataclass
class Scenario:
    name: str
    objective: str
    args: list[str] = field(default_factory=list)
    expect: dict[str, object] = field(default_factory=dict)
    seed: dict[str, str] = field(default_factory=dict)
    # Written *after* the seed commit, so the path is dirty on purpose.
    dirty_after_commit: dict[str, str] = field(default_factory=dict)
    # A capability plan written next to the repo, for scenarios that run one.
    plan: dict = field(default_factory=dict)
    timeout: float = 420.0


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""
    seconds: float = 0.0
    checks: list[tuple[str, bool, str]] = field(default_factory=list)


def _seed_repo(path: Path, files: dict[str, str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-qm", "init"],
        cwd=path,
        check=True,
    )


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=False
    ).stdout.strip()


def _run_cli(args: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("XENON_ASSUME_YES", None)  # approval must be explicit, not assumed
    return subprocess.run(
        [sys.executable, "-m", "xenon", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


def _last_event(stdout: str) -> tuple[str, str]:
    last: dict = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            last = json.loads(line)
        except json.JSONDecodeError:
            continue
    return str(last.get("kind") or ""), json.dumps(
        last.get("payload") or {}, ensure_ascii=False
    )


def _ledger_for(stdout: str, stderr: str) -> str:
    for line in reversed(stdout.splitlines() + stderr.splitlines()):
        if "ledger=" in line:
            return line.split("ledger=")[-1].strip()
    return ""


def run_scenario(
    scenario: Scenario, workdir: Path, model: str, out: Path
) -> Result:
    """One scenario.  A plan-file scenario passes no positional objective: the
    CLI rejects `--plan` together with a natural-language task."""
    repo = workdir / scenario.name
    if repo.exists():
        shutil.rmtree(repo, ignore_errors=True)
    _seed_repo(repo, scenario.seed)
    plan_arg: list[str] = []
    if scenario.plan:
        plan_path = workdir / f"{scenario.name}-plan.json"
        plan_path.write_text(
            json.dumps(scenario.plan, ensure_ascii=False), encoding="utf-8"
        )
        plan_arg = ["--plan", str(plan_path)]
    for path, content in scenario.dirty_after_commit.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    started = time.time()
    try:
        argv = ["exec"]
        if scenario.objective:
            argv.append(scenario.objective)
        argv += ["-m", model, "--workspace", str(repo)] + plan_arg + scenario.args
        proc = _run_cli(argv, cwd=ROOT, timeout=scenario.timeout)
    except subprocess.TimeoutExpired:
        return Result(
            scenario.name, FAIL, f"timeout after {scenario.timeout}s", time.time() - started
        )
    elapsed = round(time.time() - started, 1)

    artifact = out / scenario.name
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "stdout.txt").write_text(proc.stdout, encoding="utf-8")
    (artifact / "stderr.txt").write_text(proc.stderr, encoding="utf-8")

    checks: list[tuple[str, bool, str]] = []
    kind, payload = _last_event(proc.stdout)
    text = proc.stdout + proc.stderr
    checks.append(("exit code", proc.returncode in (0, 1), f"rc={proc.returncode}"))

    for key, expected in scenario.expect.items():
        if key == "phase":
            ok = f'"to": "{expected}"' in payload or kind == str(expected)
            checks.append((f"phase={expected}", ok, payload[:200]))
        elif key == "file_contains":
            path, needle = expected  # type: ignore[misc]
            target = repo / path
            content = target.read_text(encoding="utf-8") if target.is_file() else ""
            checks.append((f"{path} contains {needle!r}", needle in content, content[:200]))
        elif key == "new_commit":
            count = _git(repo, "rev-list", "--count", "HEAD")
            ok = count.isdigit() and int(count) > 1
            checks.append(("a commit was created", ok, f"count={count}"))
        elif key == "no_new_commit":
            count = _git(repo, "rev-list", "--count", "HEAD")
            ok = count.isdigit() and int(count) == 1
            checks.append(("no commit was created", ok, f"count={count}"))
        elif key == "tests_pass":
            test = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--import-mode=importlib"],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=180,
            )
            checks.append(("pytest passes", test.returncode == 0, test.stdout[-300:]))
        elif key == "blocked_names_stray_path":
            ok = "notes.txt" in text and ("unverified" in text or "blocked" in text)
            checks.append(("reason names the stray path", ok, text[-300:]))
        elif key == "blocked_or_approval":
            ok = any(
                marker in text
                for marker in (
                    "no approval provider",
                    "requires approval",
                    "requires a grant",
                    "blocked",
                )
            )
            checks.append(("blocked fail-closed with a reason", ok, text[-300:]))
        elif key == "no_file_written":
            changed = [
                line
                for line in _git(repo, "status", "--porcelain").splitlines()
                if line.strip()
            ]
            checks.append(("read-only turn wrote nothing", not changed, str(changed)[:300]))

    ledger = _ledger_for(proc.stdout, proc.stderr)
    if ledger and Path(ledger).is_file():
        shutil.copy2(ledger, artifact / "ledger.jsonl")
        replay = _run_cli(["metrics", ledger], cwd=ROOT, timeout=120)
        (artifact / "metrics.txt").write_text(
            replay.stdout + replay.stderr, encoding="utf-8"
        )

    failed = [name for name, ok, _ in checks if not ok]
    if proc.returncode not in (0, 1):
        status = FAIL
        detail = f"cli exited {proc.returncode}: {text[-400:]}"
    elif failed:
        status = FAIL
        detail = "failed: " + ", ".join(failed)
    else:
        status = PASS
        detail = f"{len(checks)} checks"
    return Result(scenario.name, status, detail, elapsed, checks)


def scenarios() -> list[Scenario]:
    return [
        Scenario(
            name="edit-commit",
            objective=(
                "在 calc.py 里添加 sub(a, b) 返回 a-b，并在 tests/test_calc.py 加一个测试，"
                "然后运行 pytest 确认通过"
            ),
            args=["--permission", "bypass"],
            seed={"calc.py": CALC, "tests/test_calc.py": TEST_CALC},
            expect={
                "phase": "finalized",
                "file_contains": ("calc.py", "def sub"),
                "tests_pass": None,
                "new_commit": None,
            },
        ),
        Scenario(
            name="plan-execute",
            objective="给 calc.py 增加 mul(a, b) 并补测试，先规划再执行，最后运行 pytest",
            args=["--permission", "bypass", "--mode", "plan-execute"],
            seed={"calc.py": CALC, "tests/test_calc.py": TEST_CALC},
            expect={
                "phase": "finalized",
                "file_contains": ("calc.py", "def mul"),
                "tests_pass": None,
            },
        ),
        Scenario(
            name="read-only-no-write",
            objective="只读分析 calc.py 的结构并说明它做什么，不要修改任何文件",
            args=["--permission", "read-only"],
            seed={"calc.py": CALC, "tests/test_calc.py": TEST_CALC},
            expect={"file_contains": ("calc.py", "def add"), "no_file_written": None},
        ),
        Scenario(
            name="no-finalize",
            objective="在 calc.py 里添加 neg(a) 返回 -a",
            args=["--permission", "bypass", "--no-finalize"],
            seed={"calc.py": CALC},
            expect={
                "file_contains": ("calc.py", "def neg"),
                "no_new_commit": None,
            },
        ),
        Scenario(
            name="accept-debt",
            objective="在 calc.py 里添加 zero() 返回 0",
            args=["--permission", "bypass", "--accept-debt"],
            seed={"calc.py": CALC},
            expect={"file_contains": ("calc.py", "def zero")},
        ),
        Scenario(
            name="plan-file-executes",
            objective="",  # the plan file carries the objective
            # A plan *file* with free-form steps: before this worked, the run
            # had a planner but no actor, so every step was marked violated.
            args=["--permission", "bypass"],
            plan={
                "objective": "add sub(a,b) to calc.py and verify the tests pass",
                "steps": [
                    {
                        "id": 1,
                        "kind": "edit",
                        "targets": ["calc.py"],
                        "expects": [
                            {"id": "e1", "kind": "file_changed", "target": "calc.py"}
                        ],
                    },
                    {
                        "id": 2,
                        "kind": "verify",
                        "expects": [
                            {
                                "id": "e2",
                                "kind": "test_passed",
                                "target": "tests/test_calc.py",
                            }
                        ],
                    },
                ],
            },
            seed={"calc.py": CALC, "tests/test_calc.py": TEST_CALC},
            expect={
                "phase": "finalized",
                "file_contains": ("calc.py", "def sub"),
                "tests_pass": None,
            },
        ),
        Scenario(
            name="stray-change-reported",
            objective="在 calc.py 里加 inc(a) 返回 a+1",
            args=["--permission", "bypass"],
            seed={
                "calc.py": CALC,
                "tests/test_calc.py": TEST_CALC,
                "notes.txt": "committed" + chr(10),
            },
            # A pre-existing, unrelated *dirty* file: it must be refused, and
            # the reason must name the path (a generic "semantic claim"
            # message would be useless to the operator).  The file is committed
            # by the seed and only then modified, so it is dirty on purpose.
            dirty_after_commit={"notes.txt": "committed" + chr(10) + "dirty" + chr(10)},
            expect={"blocked_names_stray_path": None},
        ),
        Scenario(
            name="network-needs-approval",
            objective="用 github 搜索一下 xianyu-sheng/Xenon 这个仓库是什么语言写的",
            # No --permission bypass: headless has nobody to approve, so this
            # must stop with a reason rather than hang or silently pass.
            args=["--max-steps", "3"],
            seed={"calc.py": CALC},
            expect={"blocked_or_approval": None},
        ),
        Scenario(
            name="impossible-task",
            objective="把 /nonexistent/path/xyz.py 里的 foo 改名为 bar",
            args=["--permission", "bypass", "--max-steps", "3"],
            seed={"calc.py": CALC},
            expect={},
        ),
    ]


SDK_SNIPPET = """
import json, os, sys
os.chdir(sys.argv[1])
import xenon
out = xenon.run_task_result(
    sys.argv[2],
    model_priority=[sys.argv[3]],
    workspace=os.getcwd(),
    permission=sys.argv[4],
)
print("OUTCOME " + json.dumps({
    "accepted": out.accepted,
    "phase": out.phase,
    "evidence": out.evidence,
    "debt": list(out.debt),
    "reason": out.reason,
    "answer": str(out)[:200],
}, ensure_ascii=False))
"""


def run_sdk_scenario(name: str, objective: str, permission: str, expect: dict, workdir: Path, model: str, out: Path) -> Result:
    """Drive Xenon as a library, the way a script would."""

    repo = workdir / name
    if repo.exists():
        shutil.rmtree(repo, ignore_errors=True)
    _seed_repo(repo, {"calc.py": CALC, "tests/test_calc.py": TEST_CALC})
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", SDK_SNIPPET, str(repo), objective, model, permission],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=420,
            cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired:
        return Result(name, FAIL, "timeout", time.time() - started)

    artifact = out / name
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "stdout.txt").write_text(proc.stdout, encoding="utf-8")
    (artifact / "stderr.txt").write_text(proc.stderr, encoding="utf-8")

    payload: dict = {}
    for line in proc.stdout.splitlines():
        if line.startswith("OUTCOME "):
            payload = json.loads(line[len("OUTCOME "):])
    checks: list[tuple[str, bool, str]] = []
    checks.append(("sdk ran", bool(payload), (proc.stderr or proc.stdout)[-300:]))
    for key, expected in expect.items():
        if key == "accepted":
            checks.append(
                (f"accepted={expected}", payload.get("accepted") is expected, str(payload)[:200])
            )
        elif key == "evidence":
            checks.append(
                (f"evidence={expected}", payload.get("evidence") == expected, str(payload)[:200])
            )
        elif key == "has_reason":
            checks.append(
                ("blocked run explains itself", bool(payload.get("reason")), str(payload.get("reason"))[:200])
            )
        elif key == "file_contains":
            path, needle = expected  # type: ignore[misc]
            target = repo / path
            content = target.read_text(encoding="utf-8") if target.is_file() else ""
            checks.append((f"{path} contains {needle!r}", needle in content, content[:150]))

    failed = [n for n, ok, _ in checks if not ok]
    status = PASS if not failed else FAIL
    detail = f"{len(checks)} checks" if not failed else "failed: " + ", ".join(failed)
    return Result(name, status, detail, round(time.time() - started, 1), checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="deepseek/deepseek-v4-flash")
    parser.add_argument("--only", default="", help="comma separated scenario names")
    parser.add_argument("--out", default="", help="artifact directory")
    args = parser.parse_args(argv)

    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="xenon-e2e-"))
    out.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="xenon-e2e-work-"))

    wanted = {item.strip() for item in args.only.split(",") if item.strip()}
    selected = [s for s in scenarios() if not wanted or s.name in wanted]

    sdk_cases = [
        (
            "sdk-default-blocks-shell",
            "在 calc.py 里加 e() 返回 5，并运行 python 验证",
            "auto-edit",
            {"accepted": False, "has_reason": None},
        ),
        (
            "sdk-bypass-delivers",
            "在 calc.py 里加 f() 返回 6，并在 tests/test_calc.py 加一个测试",
            "bypass",
            {"accepted": True, "file_contains": ("calc.py", "def f")},
        ),
    ]

    results: list[Result] = []
    for name, objective, permission, expect in sdk_cases:
        if wanted and name not in wanted:
            continue
        print("" + chr(10) + f"=== {name} (sdk) ...", flush=True)
        result = run_sdk_scenario(name, objective, permission, expect, workdir, args.model, out)
        results.append(result)
        print(f"    {result.status} ({result.seconds}s) {result.detail}", flush=True)
        for check_name, ok, detail in result.checks:
            print(f"      [{'ok' if ok else 'XX'}] {check_name}: {detail[:120]}", flush=True)

    for scenario in selected:
        print(f"\n=== {scenario.name} ...", flush=True)
        result = run_scenario(scenario, workdir, args.model, out)
        results.append(result)
        print(f"    {result.status} ({result.seconds}s) {result.detail}", flush=True)
        for name, ok, detail in result.checks:
            print(f"      [{'ok' if ok else 'XX'}] {name}: {detail[:120]}", flush=True)

    report = {
        "model": args.model,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "workdir": str(workdir),
        "results": [
            {
                "name": r.name,
                "status": r.status,
                "detail": r.detail,
                "seconds": r.seconds,
                "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in r.checks],
            }
            for r in results
        ],
    }
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    passed = sum(1 for r in results if r.status == PASS)
    print(f"\n{passed}/{len(results)} scenarios passed; artifacts -> {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())