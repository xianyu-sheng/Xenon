"""Real probe: can a child's run be traced back to its parent?

Runs one real child with ``parent_task_id="demo-parent"`` and then asks three
questions of the artifacts on disk:

  1. does the child ledger mention its parent at all?
  2. can the parent's ledger be found and does it mention the child?
  3. after a fresh process (no in-memory session), is the tree still derivable?
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

from xenon.harness.child import run_child_task  # noqa: E402
from xenon.harness.llm import resolve_model_config  # noqa: E402
from xenon.kernel.policies import PermissionMode  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-flash"
PARENT = "demo-parent-" + uuid.uuid4().hex[:6]


def main() -> None:
    root = Path("/tmp/tree-" + uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    (root / "note.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )

    outcome = run_child_task(
        objective="Add a docstring to note.py. Do not change VALUE.",
        workspace=root,
        model=resolve_model_config([MODEL]),
        profile="react",
        parent_task_id=PARENT,
        ledger_dir=Path("/tmp/treel-" + uuid.uuid4().hex[:6]),
        permission=PermissionMode.AUTO_EDIT,
        max_steps=6,
        timeout=180,
    )
    print(f"parent_task_id={PARENT}")
    print(f"child={outcome.child_id} phase={outcome.phase} ledger={outcome.ledger_path}")

    ledger = Path(outcome.ledger_path)
    payloads = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    started = next(p for p in payloads if p["kind"] == "task_started")

    mentions = [k for k, v in started["payload"].items() if PARENT in str(v)]
    print("child ledger task_started fields:", sorted(started["payload"]))
    print("child ledger mentions parent in:", mentions or "NOTHING")

    any_parent = [
        p["kind"] for p in payloads if PARENT in json.dumps(p, ensure_ascii=False)
    ]
    print("any event mentioning the parent:", any_parent or "NOTHING")

    parent_ledger = Path.home() / ".xenon" / "ledger" / f"{PARENT}.jsonl"
    print("parent ledger exists:", parent_ledger.exists())
    if parent_ledger.exists():
        text = parent_ledger.read_text(encoding="utf-8")
        print("parent ledger mentions child:", outcome.child_id in text)

    subprocess.run(["git", "status", "--porcelain"], cwd=root)


if __name__ == "__main__":
    main()
