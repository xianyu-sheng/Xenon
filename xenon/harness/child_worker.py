"""Child worker entry point: one delegated task, one process, one ledger.

Started by :func:`xenon.harness.child.run_child_task` as::

    python -m xenon.harness.child_worker <spec.json> <outcome.json>

The spec/outcome files are the *only* channel, so the parent never parses
stdout and a crash cannot be mistaken for a result.  Everything the parent needs
to know is either in the outcome file or in the child's own ledger.

Why a process and not a thread: a thread cannot be killed.  A child that is
abandoned at its deadline keeps running — it was observed writing files *after*
the parent recorded a timeout, which makes the parent's own delivery claim
unreliable.  A process can be killed, and killing it is observable.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from xenon.harness.llm import ModelConfig
from xenon.harness.runner import run_task
from xenon.kernel.policies import PermissionMode
from xenon.kernel.strategies import LoopProfile

# Mirrors ``xenon.harness.child.CHILD_PROFILES``.  Duplicated deliberately: the
# worker must stay importable without the parent module (a child process has no
# parent state), and the parent validates the name before spawning anyway.
PROFILES: dict[str, LoopProfile] = {
    "react": LoopProfile(),
    "plan-execute": LoopProfile(plan=True, step_scope=True, follow_plan=True),
    "plan-react": LoopProfile(plan=True, step_scope=True, step_calls=6),
    "kernel": LoopProfile(plan=True, step_scope=True, step_calls=6),
    "reflection": LoopProfile(reflect_rounds=1, reflect_calls=1),
}


def model_from_spec(raw: dict[str, Any]) -> ModelConfig:
    """Rebuild the model config the parent resolved.

    Hooks (usage/cancel/progress sinks) are deliberately dropped: they are
    parent-process callbacks and cannot travel across the boundary.  A child's
    spend is reported through its own ledger instead.
    """

    return ModelConfig(
        model_id=str(raw["model_id"]),
        base_url=str(raw.get("base_url") or ""),
        api_key=str(raw.get("api_key") or ""),
        max_tokens=int(raw.get("max_tokens") or 8192),
        temperature=float(raw.get("temperature") or 0.0),
        reasoning_effort=str(raw.get("reasoning_effort") or ""),
        context_window=int(raw.get("context_window") or 0),
    )


def model_to_spec(config: ModelConfig) -> dict[str, Any]:
    """The serialisable half of a :class:`ModelConfig`."""

    return {
        "model_id": config.model_id,
        "base_url": config.base_url,
        "api_key": config.api_key,
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
        "reasoning_effort": config.reasoning_effort,
        "context_window": config.context_window,
    }


def run_spec(spec: dict[str, Any], *, client: Any = None) -> dict[str, Any]:
    """Execute one spec in this process and return the outcome payload.

    ``client`` is a testing seam: a fake transport lets the payload shape be
    exercised here rather than only inside a spawned process, where the parent's
    coverage cannot see it.
    """

    from xenon.harness.llm import build_llm_loop_strategy

    profile_name = str(spec.get("profile") or "kernel")
    if profile_name not in PROFILES:
        raise KeyError(f"unknown child profile: {profile_name}")
    strategy = build_llm_loop_strategy(
        model_from_spec(spec["model"]), PROFILES[profile_name], client=client
    )

    run = run_task(
        str(spec["objective"]),
        user_objective=str(spec["objective"]),
        strategy=strategy,
        workspace=spec["workspace"],
        ledger_dir=spec["ledger_dir"],
        task_id=str(spec["child_id"]),
        permission=PermissionMode(str(spec.get("permission") or "auto-edit")),
        # The parent (or the user) owns delivery; a child must not commit.
        finalize=False,
        max_steps=int(spec.get("max_steps") or 20),
    )
    result = run.result
    return {
        "child_id": str(spec["child_id"]),
        "accepted": result.accepted,
        "phase": result.phase.value,
        "answer": result.answer,
        "ledger_path": str(run.ledger_path),
        "debt": [item.reason for item in result.debt],
        "tool_calls": int(result.metrics.get("tool_calls", 0.0)),
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: python -m xenon.harness.child_worker <spec> <outcome>", file=sys.stderr)
        return 2
    spec_path, out_path = Path(argv[0]), Path(argv[1])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    payload = run_spec(spec)
    # Write then rename: the parent must never read a half-written outcome.
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
