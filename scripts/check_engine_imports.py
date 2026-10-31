#!/usr/bin/env python3
"""Engine-skeleton ratchet (#202).

The *skeleton* (the seven engine classes, their registry/base/prompts and the
DAG plan machinery) is being deleted. Organs that live under ``xenon.engine`` but
are still used by nodes/REPL — ``context``, ``callbacks``, ``tool_tracker``,
``task_state``, ``evidence_*``, ``loop_detector``, ``circuit_breaker``, ``budget``,
``hollow_detector``, ``structured_compactor``, ``execution_evidence``,
``tool_runtime`` … — are **not** part of this guard.

Production is fully decoupled: no module under ``xenon/`` imports the skeleton.
The remaining ~100 test modules still use the engines as a harness; they are
migrated or removed module-by-module while this guard keeps production clean.
``xenon run <workflow.yaml>`` is **not** a skeleton consumer: it runs the
workflow DAG scheduler over ``nodes/``, which is an organ that stays.

This guard keeps that coupling explicit and non-growing. It only shrinks; never
add a file here to make a new violation pass.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKELETON = (
    "base",
    "registry",
    "builtin_engines",
    "react_engine",
    "plan_execute_engine",
    "reflection_engine",
    "combined_engines",
    "event_bus",
    "plan_dag",
    "plan_dag_executor",
    "plan_contract",
    "react_prompts",
    "execution_prompts",
    "strategy_guide",
    "scout",
    "coding_contract",
    "verification_nudge",
)

# Nothing in production may import the skeleton any more.  If this dict ever
# grows again, the deletion has regressed.
ALLOWED: dict[str, str] = {}

# The live turn path must never reference the skeleton again.
TURN_PATH = ("xenon/repl/turn_flow.py", "xenon/repl/runtime_adapter.py")

IMPORT_RE = re.compile(r"xenon\.engine\." + r"(" + "|".join(SKELETON) + r")\b")


def main() -> int:
    violations: list[str] = []
    for path in (ROOT / "xenon").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("xenon/engine/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not IMPORT_RE.search(text):
            continue
        if rel in ALLOWED:
            continue
        violations.append(rel)

    for rel in TURN_PATH:
        target = ROOT / rel
        if not target.is_file():
            continue
        if IMPORT_RE.search(target.read_text(encoding="utf-8")):
            violations.append(f"{rel} (turn path)")

    if violations:
        print("engine-skeleton guard: new importers detected")
        for item in sorted(set(violations)):
            print(f"  - {item}")
        print(
            "\nThe skeleton is being deleted. Migrate the caller instead of "
            "extending scripts/check_engine_imports.py."
        )
        return 1

    print("engine-skeleton guard OK")
    for rel, why in sorted(ALLOWED.items()):
        if (ROOT / rel).exists():
            print(f"  allowed: {rel} — {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
