"""Real probe: self-evolution slice 3 — lessons are *injected*, not recorded.

A store pre-loaded with one pitfall lesson, a real task.  Checks the two
things the user cares about:

    1. the lesson actually rides into the model's view (constraint in the
       system prompt), not just into a file;
    2. it is bounded — one lesson in, one constraint, no lesson dump.

Reproduce:

    PYTHONIOENCODING=utf-8 python docs/evidence/self-evolution/applier_probe.py \\
        deepseek/deepseek-v4-flash
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

from xenon.harness.lessons import LessonStore
from xenon.harness.llm import ModelConfig
from xenon.kernel.contracts import TaskMode, TaskSpec, Budget
from xenon.kernel.lessons import Lesson, lesson_fingerprint
from xenon.kernel.view import build_model_view
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger
from xenon.repl.runtime_adapter import RuntimeAdapter

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-flash"


def repo() -> Path:
    root = Path("/tmp/sa-" + uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    return root


def main() -> int:
    root = repo()
    store_path = Path("/tmp/sal-" + uuid.uuid4().hex[:6]) / "lessons.yaml"
    store = LessonStore(store_path)
    store.add(
        Lesson(
            id="L-DEMO",
            kind="pitfall",
            trigger_event="debt_opened",
            fingerprint=lesson_fingerprint(
                "debt_opened", {"kind": "requirement_met", "target": "x", "reason": "no receipt"}
            ),
            action_text="声称需求满足前必须存在对应工具回执",
            source_task="demo",
            last_applied_at=999999.0,  # fresh: not stale
        )
    )

    adapter = RuntimeAdapter(
        workspace=root,
        model_config=ModelConfig(model_id=MODEL),
        ledger_dir=Path("/tmp/sal-" + uuid.uuid4().hex[:6]),
        finalize=False,
        max_steps=8,
    )
    # The REPL adapter does not carry a lesson store yet (slice 4 wires /lessons
    # and the adapter); drive run_task directly for the probe.
    from xenon.catalog.catalog import build_catalog
    from xenon.harness.llm import build_llm_loop_strategy
    from xenon.harness.runner import run_task
    from xenon.kernel.policies import PermissionMode
    from xenon.kernel.strategies import LoopProfile

    strategy = build_llm_loop_strategy(
        ModelConfig(model_id=MODEL), LoopProfile(), client=None
    )
    captured: list = []
    run = run_task(
        "Add a subtract function to calc.py, write a test, run it, and report.",
        strategy=strategy,
        workspace=root,
        ledger_dir=Path("/tmp/sal-" + uuid.uuid4().hex[:6]),
        task_id="applier-probe",
        permission=PermissionMode.AUTO_EDIT,
        finalize=False,
        max_steps=8,
        lesson_store=store,
        on_runtime=captured.append,
    )
    ledger = Ledger(run.ledger_path, "applier-probe")
    events = list(ledger.read())
    applied = [
        event for event in events if event.kind is EventKind.LESSON_APPLIED
    ]
    runtime = captured[0] if captured else None
    spec = runtime._spec if runtime is not None else None
    view = build_model_view(
        spec=spec or TaskSpec(
            task_id="applier-probe", objective="x", workspace=str(root), mode=TaskMode.AUTO
        ),
        catalog=build_catalog("core"),
        ledger=ledger,
    )

    print(f"task phase: {run.result.phase.value}")
    print(f"lessons applied: {len(applied)} "
          f"-> {[e.payload['lesson_id'] for e in applied]}")
    lesson_constraints = [
        line for line in view.system.splitlines()
        if line.startswith("- [lesson")
    ]
    print(f"lesson constraints in the model view: {len(lesson_constraints)}")
    for line in lesson_constraints[:5]:
        print(f"  {line}")
    if len(applied) != 1 or len(lesson_constraints) != 1:
        print("FAIL: expected exactly one injected lesson, one constraint")
        return 1
    if "[lesson L-DEMO]" not in view.system:
        print("FAIL: the lesson did not reach the system prompt")
        return 1
    print("PASS: one lesson injected into the model view — used, and bounded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
