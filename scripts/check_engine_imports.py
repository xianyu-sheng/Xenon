#!/usr/bin/env python3
"""Engine-skeleton guard (#202).

The seven engine classes and their support modules are **deleted**.  What
survives under ``xenon/engine/`` are organs — ``context``, ``callbacks``,
``tool_tracker``, ``task_state``, ``evidence_*``, ``budget``,
``structured_compactor``, ``tool_runtime``, ``scheduler`` … — which are still
used by nodes/REPL and are not part of this guard.

The guard enforces two things:

1. **The skeleton files must not come back.** Referencing or re-creating any of
   them fails, including from tests and evals: a test that needs an engine to
   exist means the architecture is regressing, not that the test is fine.
2. **The live turn path never touches the engine package** for loop decisions.
   ``/mode`` selects a ``LoopProfile``; it must not reach for an engine class.

The list below only shrinks. Adding a name to make a new violation pass is the
one thing this file exists to prevent.
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
    "verification_nudge",
    "claim_gate",
    "execution_policy",
)

# Nothing anywhere may import the skeleton.  If this dict grows, the deletion
# has regressed.
ALLOWED: dict[str, str] = {}

# The live turn path must never dispatch to an engine again.
TURN_PATH = ("xenon/repl/turn_flow.py", "xenon/repl/runtime_adapter.py")

# A real import statement, not a string literal or a guard-test assertion
# (``assert "xenon.engine.base" not in sys.modules``).  Requiring the line to
# begin with ``import``/``from`` avoids allowlisting files that mention the
# names on purpose.
_NAMES = "|".join(SKELETON)
IMPORT_RE = re.compile(
    r"^[ \t]*(?:from|import)\s+xenon\.engine\.(?:" + _NAMES + r")\b",
    re.MULTILINE,
)

# Scanned trees: tests and evals count.  A test that imports a deleted engine
# is either stale or hiding a regression.
SCAN_DIRS = ("xenon", "tests", "evals", "scripts")


def _scan() -> tuple[list[str], list[str]]:
    violations: list[str] = []
    present: list[str] = []
    for name in SKELETON:
        target = ROOT / "xenon" / "engine" / f"{name}.py"
        if target.is_file():
            present.append(target.relative_to(ROOT).as_posix())

    for folder in SCAN_DIRS:
        base = ROOT / folder
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
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
    return violations, present


def main() -> int:
    violations, present = _scan()

    for rel in TURN_PATH:
        target = ROOT / rel
        if not target.is_file():
            continue
        if IMPORT_RE.search(target.read_text(encoding="utf-8")):
            violations.append(f"{rel} (turn path)")

    if present:
        print("engine-skeleton guard: deleted skeleton files are back")
        for item in sorted(present):
            print(f"  - {item}")
        return 1

    if violations:
        print("engine-skeleton guard: skeleton importers detected")
        for item in sorted(set(violations)):
            print(f"  - {item}")
        print(
            "\nThe skeleton is gone. Migrate the caller (or drop the test) "
            "instead of extending scripts/check_engine_imports.py."
        )
        return 1

    print("engine-skeleton guard OK")
    print(f"  skeleton files checked: {len(SKELETON)} (all absent)")
    print(f"  trees scanned: {', '.join(SCAN_DIRS)}")
    for rel, why in sorted(ALLOWED.items()):
        if (ROOT / rel).exists():
            print(f"  allowed: {rel} — {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
