"""CLI for ``xenon exec`` (#203, #220).

Two entry shapes share the same kernel path:

* ``xenon exec --plan plan.json`` — deterministic capability plan.
* ``xenon exec "objective" -m provider/model`` — ActFirst LLM strategy.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from xenon.harness.llm import (
    ModelConfigError,
    build_llm_loop_strategy,
    resolve_model_config,
)
from xenon.harness.plan import PlanError, parse_plan
from xenon.harness.runner import JsonlSink, run_plan, run_task, write_result
from xenon.kernel.policies import PermissionMode
from xenon.kernel.strategies import DEFAULT_PROFILE


def _load_plan(path: str) -> Any:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PlanError("plan root must be an object")
    return parse_plan(payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xenon exec",
        description=(
            "Run a capability plan or a natural-language task through the kernel."
        ),
    )
    parser.add_argument("task", nargs="?", default=None, help="natural-language task")
    parser.add_argument("--task", dest="task_option", default=None)
    parser.add_argument("--plan", default=None, help="JSON plan file")
    parser.add_argument("--workspace", default=".", help="workspace root")
    parser.add_argument("--ledger-dir", default=None)
    parser.add_argument("--task-id", default=None)
    parser.add_argument(
        "-m",
        "--model",
        nargs="+",
        default=None,
        help="model id or models.yaml alias (default: configured model)",
    )
    parser.add_argument("--config", default=None, help="models.yaml path")
    parser.add_argument(
        "--accept-debt",
        action="store_true",
        help="deliver despite unverified gaps (evidence stays PARTIAL)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 unless the answer is verified (CI gate)",
    )
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


def _task_text(args: argparse.Namespace) -> str | None:
    # `--task` is the explicit form, so it wins.  Reading the positional first
    # meant `xenon ... cli exec --task "real work"` ran the literal word
    # "exec" as the objective, and the model dutifully planned around it.
    value = (
        getattr(args, "task_option", None)
        or getattr(args, "task", None)
        or getattr(args, "workflow_path", None)
    )
    text = str(value).strip() if value is not None else ""
    return text or None


def run_from_args(args: argparse.Namespace) -> int:
    plan_path = getattr(args, "plan", None)
    task = _task_text(args)
    if plan_path and task:
        print("plan error: --plan and a task are mutually exclusive", file=sys.stderr)
        return 2
    if not plan_path and not task:
        print("plan error: provide --plan or a natural-language task", file=sys.stderr)
        return 2

    plan = None
    if plan_path:
        try:
            plan = _load_plan(plan_path)
        except (OSError, ValueError, PlanError) as exc:
            print(f"plan error: {exc}", file=sys.stderr)
            return 2
    # A *deterministic* plan (every step capability-bound) runs without a
    # model; a free-form plan needs one for its step actor.  So resolve
    # lazily: failing to find a model is only fatal when something needs it.
    try:
        model_config = resolve_model_config(
            getattr(args, "model", None),
            config_path=getattr(args, "config", None),
        )
    except ModelConfigError as exc:
        if plan is None:
            print(f"model error: {exc}", file=sys.stderr)
            return 2
        model_config = None

    sink = JsonlSink(sys.stdout) if args.jsonl else None
    try:
        from xenon.sandbox.backends import build_runtime, spec_from_env

        # Where commands run is decided once per process.  A configured
        # backend that is unavailable fails closed here, before any turn.
        tool_runtime = build_runtime(
            args.workspace or ".",
            getattr(args, "sandbox", None) or spec_from_env(),
        )
        if plan is not None:
            run = run_plan(
                plan,
                workspace=args.workspace or ".",
                ledger_dir=args.ledger_dir,
                task_id=args.task_id,
                permission=PermissionMode(args.permission or "auto-edit"),
                finalize=not args.no_finalize,
                max_steps=args.max_steps,
                sink=sink,
                model_config=model_config,
                tool_runtime=tool_runtime,
            )
        else:
            assert task is not None and model_config is not None
            run = run_task(
                task,
                strategy=build_llm_loop_strategy(model_config, DEFAULT_PROFILE),
                workspace=args.workspace or ".",
                ledger_dir=args.ledger_dir,
                task_id=args.task_id,
                permission=PermissionMode(args.permission or "auto-edit"),
                finalize=not args.no_finalize,
                max_steps=args.max_steps,
                constraints=(f"model:{model_config.model_id}",),
                sink=sink,
                accept_debt=bool(getattr(args, "accept_debt", False)),
                tool_runtime=tool_runtime,
            )
    except Exception as exc:  # noqa: BLE001 — CLI must return a code
        from xenon.utils.errors import envelope

        problem = envelope(exc)
        print(
            f"harness error [{problem.kind}/{problem.code}"
            f"{', retryable' if problem.retryable else ''}]: {problem.message}",
            file=sys.stderr,
        )
        return 3
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as stream:
            write_result(run.result, stream)
    result = run.result
    # Every run leaves a reportable pair next to its ledger.  `xenon report`
    # reads this sibling when the answer lives outside a `delivered` event
    # (blocked runs never append one), so a CI job does not need to guess the
    # --output path.
    try:
        sibling = run.ledger_path.with_suffix(".result.json")
        with sibling.open("w", encoding="utf-8") as stream:
            write_result(result, stream)
    except OSError:
        pass
    print(
        f"{result.phase.value} evidence={result.evidence_status.value} "
        f"sha={result.finalize_sha or '-'} gaps={len(result.debt)} "
        f"ledger={run.ledger_path}",
        file=sys.stderr,
    )
    # Evidence is a label, not a gate: a completed run exits 0 even with gaps.
    # `--strict` restores the gate for CI, where a non-verified answer should
    # fail the job.
    if getattr(args, "strict", False) and result.evidence_status.value != "verified":
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return run_from_args(_parser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
