"""Real probe: is one dispatch distinguishable from several? (#361)

Dispatches two batches of real children and then asks the ledgers alone:
which children belonged together, and did the batches stay separate?
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

from xenon.harness.child import run_child_tasks  # noqa: E402
from xenon.harness.llm import resolve_model_config  # noqa: E402
from xenon.kernel.policies import PermissionMode  # noqa: E402
from xenon.replay import children_of, render_children  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-flash"


def main() -> None:
    root = Path("/tmp/batch-" + uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    for name in ("a.py", "b.py"):
        (root / name).write_text(f"VALUE_{name[0].upper()} = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    ledger_dir = Path("/tmp/batchl-" + uuid.uuid4().hex[:6])

    def dispatch(objectives: list[str]) -> None:
        outcomes = run_child_tasks(
            objectives=objectives,
            workspace=root,
            model=resolve_model_config([MODEL]),
            profile="react",
            parent_task_id="batch-parent",
            ledger_dir=ledger_dir,
            permission=PermissionMode.READ_ONLY,
            max_steps=4,
            timeout=180,
            max_workers=2,
        )
        for outcome in outcomes:
            print(f"  {outcome.child_id}  batch={outcome.batch_id}  phase={outcome.phase}")

    print("--- dispatch 1 (two children) ---")
    dispatch(["Report the constant in a.py.", "Report the constant in b.py."])
    print("--- dispatch 2 (one child) ---")
    dispatch(["Report how many .py files are here."])

    print("\n--- batch_id recorded in each child ledger ---")
    for path in sorted(ledger_dir.glob("*.jsonl")):
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        started = next(e for e in events if e["kind"] == "task_started")
        print(f"  {path.stem}: batch_id={started['payload'].get('batch_id')!r}")

    print("\n--- xenon replay --children batch-parent ---")
    print(render_children(children_of("batch-parent", root=ledger_dir), parent="batch-parent"))


if __name__ == "__main__":
    main()
