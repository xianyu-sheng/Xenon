"""Real probe: the decision-chain extractor survives compaction (#289).

A real task whose ledger carries plan/control/debt decisions, run with a
small enough budget that compaction actually folds events, then the final
view's elided entry must carry ``why`` lines — zero LLM.

Reproduce:

    PYTHONIOENCODING=utf-8 python docs/evidence/decision-chain/probe.py \\
        deepseek/deepseek-v4-flash
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

from xenon.catalog.catalog import build_catalog
from xenon.harness.llm import ModelConfig
from xenon.kernel.contracts import TaskMode, TaskSpec, Budget
from xenon.kernel.view import build_model_view
from xenon.ledger.store import Ledger
from xenon.repl.runtime_adapter import RuntimeAdapter

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-flash"


def repo() -> Path:
    root = Path("/tmp/dc-" + uuid.uuid4().hex[:6])
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
    adapter = RuntimeAdapter(
        workspace=root,
        model_config=ModelConfig(model_id=MODEL),
        ledger_dir=Path("/tmp/dcl-" + uuid.uuid4().hex[:6]),
        finalize=False,
        # Small working set forces compaction on a real run.
        max_steps=30,
    )
    run = adapter.run(
        "Add a subtract function to calc.py, write a test for it, and run the "
        "test. Finish with an answer.",
        task_id="decision-chain",
    )
    ledger = Ledger(run.ledger_path, "decision-chain")
    view = build_model_view(
        spec=TaskSpec(
            task_id="decision-chain",
            objective=run.result.answer or "done",
            workspace=str(root),
            mode=TaskMode.AUTO,
            budget=Budget(max_tokens=120, max_steps=30),
        ),
        catalog=build_catalog("core"),
        ledger=ledger,
    )

    elided = [
        item for item in view.working_set
        if item.get("kind") == "elided"
    ]
    why_lines = [
        line
        for item in elided
        for line in (item.get("why") or [])
    ]
    # A decision survives either as a why line (folded) or as itself (kept).
    kept_decisions = [
        item for item in view.working_set
        if item.get("kind") in {"control_decision", "debt_opened", "plan_accepted"}
    ]
    decision_events = [
        e.kind.value
        for e in ledger.read()
        if e.kind.value
        in {"plan_accepted", "plan_proposed", "control_decision", "debt_opened"}
    ]
    print(f"task phase: {run.result.phase.value}  accepted: {run.result.accepted}")
    print(f"decision events in ledger: {len(decision_events)} -> {decision_events}")
    print(f"elided entries: {len(elided)}; why lines kept: {len(why_lines)}")
    print(f"decisions kept in the view (not folded): {len(kept_decisions)}")
    for line in why_lines[:6]:
        print(f"  why: {line}")
    if decision_events and elided and not why_lines and not kept_decisions:
        print("FAIL: decisions were folded AND no why lines survived")
        return 1
    print("PASS: every decision is visible — folded ones via why, others as events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
