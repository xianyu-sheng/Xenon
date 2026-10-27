"""CLI for ``xenon exec --plan`` (#203 first slice)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from xenon.harness.plan import PlanError, parse_plan
from xenon.harness.runner import JsonlSink, run_plan, write_result
from xenon.kernel.policies import PermissionMode


def _load_plan(path: str) -> Any:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PlanError("plan root must be an object")
    return parse_plan(payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xenon exec",
        description="Run a deterministic, capability-based plan through the kernel.",
    )
    parser.add_argument("--plan", required=True, help="JSON plan file")
    parser.add_argument("--workspace", default=".", help="workspace root")
    parser.add_argument("--ledger-dir", default=None)
    parser.add_argument("--task-id", default=None)
    parser.add_argument(
        "--permission",
        choices=[item.value for item in PermissionMode],
        default=PermissionMode.AUTO_EDIT.value,
    )
    parser.add_argument(
        "--jsonl",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="emit ledger events as JSONL on stdout (default: true)",
    )
    parser.add_argument("--output", default=None, help="write result JSON here")
    parser.add_argument("--no-finalize", action="store_true")
    parser.add_argument("--max-steps", type=int, default=20)
    return parser


def run_from_args(args: argparse.Namespace) -> int:
    try:
        plan = _load_plan(args.plan)
    except (OSError, ValueError, PlanError) as exc:
        print(f"plan error: {exc}", file=sys.stderr)
        return 2
    sink = JsonlSink(sys.stdout) if args.jsonl else None
    try:
        run = run_plan(
            plan,
            workspace=args.workspace or ".",
            ledger_dir=args.ledger_dir,
            task_id=args.task_id,
            permission=PermissionMode(args.permission),
            finalize=not args.no_finalize,
            max_steps=args.max_steps,
            sink=sink,
        )
    except Exception as exc:  # noqa: BLE001 — CLI must return a code
        print(f"harness error: {exc}", file=sys.stderr)
        return 3
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as stream:
            write_result(run.result, stream)
    result = run.result
    print(
        f"{result.phase.value} accepted={result.accepted} "
        f"sha={result.finalize_sha or '-'} debt={len(result.debt)} "
        f"ledger={run.ledger_path}",
        file=sys.stderr,
    )
    return 0 if result.accepted else 1


def main(argv: Sequence[str] | None = None) -> int:
    return run_from_args(_parser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
