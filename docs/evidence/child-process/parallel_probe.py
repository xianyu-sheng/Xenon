"""Real-task probe: parallel children, each in its own process.

Answers two questions with a real model:
  1. do they actually overlap in time (wall clock < serial sum)?
  2. does every child end with its own ledger and no leaked process?
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

from xenon.harness.child import run_child_tasks  # noqa: E402
from xenon.harness.llm import resolve_model_config  # noqa: E402
from xenon.kernel.policies import PermissionMode  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-flash"


def main() -> None:
    root = Path("/tmp/par-" + uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "b.py").write_text("def beta():\n    return 2\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )

    started = time.time()
    outcomes = run_child_tasks(
        objectives=[
            "Read a.py and report what function it defines. Do not modify anything.",
            "Read b.py and report what function it defines. Do not modify anything.",
        ],
        workspace=root,
        model=resolve_model_config([MODEL]),
        profile="react",
        parent_task_id="par",
        ledger_dir=Path("/tmp/parl-" + uuid.uuid4().hex[:6]),
        permission=PermissionMode.AUTO_EDIT,
        max_steps=6,
        timeout=180,
        max_workers=2,
    )
    wall = time.time() - started
    serial = sum(o.elapsed_seconds for o in outcomes)

    print(f"workspace {root}")
    print(f"wall={wall:.1f}s  serial-sum={serial:.1f}s  overlap={serial / wall:.2f}x")
    for o in outcomes:
        print(
            f"  {o.child_id} pid={o.pid} phase={o.phase} killed={o.killed} "
            f"{o.elapsed_seconds:.1f}s tools={o.tool_calls} ledger={Path(o.ledger_path).name}"
        )

    # Leak check: no child pid should survive the call.
    alive = []
    for o in outcomes:
        listing = subprocess.run(
            ["tasklist", "/FI", f"PID eq {o.pid}"], capture_output=True, text=True
        ).stdout
        if str(o.pid) in listing:
            alive.append(o.pid)
    print("leaked pids:", alive or "none")
    subprocess.run(["git", "status", "--porcelain"], cwd=root)


if __name__ == "__main__":
    main()
