"""Real probe: do two children in parallel clobber each other's writes?

Both children get the same workspace (today's behaviour), both are told to edit
the *same* file, and both run genuinely in parallel in separate processes.

The question is not "who wins" but "does the record tell the truth": what does
each child claim, and what does the workspace actually contain afterwards?
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
    root = Path("/tmp/clobber-" + uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    target = root / "shared.py"
    target.write_text(
        'def one():\n    return 1\n\n\ndef two():\n    return 2\n\n\n'
        'def three():\n    return 3\n'
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    before = target.read_text()

    started = time.time()
    outcomes = run_child_tasks(
        objectives=[
            "In shared.py, change function one() to return 111. Touch nothing else.",
            "In shared.py, change function three() to return 333. Touch nothing else.",
        ],
        workspace=root,
        model=resolve_model_config([MODEL]),
        profile="react",
        parent_task_id="clob",
        ledger_dir=Path("/tmp/clobl-" + uuid.uuid4().hex[:6]),
        permission=PermissionMode.AUTO_EDIT,
        max_steps=8,
        timeout=240,
        max_workers=2,
    )
    wall = time.time() - started

    print(f"workspace {root}")
    print(f"wall={wall:.1f}s")
    for o in outcomes:
        print(
            f"  {o.child_id} pid={o.pid} phase={o.phase} tools={o.tool_calls} "
            f"debt={list(o.debt)[:2]}"
        )

    after = target.read_text()
    print("\n--- shared.py before ---")
    print(before)
    print("--- shared.py after ---")
    print(after)

    # Isolation: each child worked in its own worktree; the parent applies the
    # patches deliberately (never automatically).
    from xenon.harness.child_worktree import apply_patch

    for o in outcomes:
        patch = Path(o.patch_path).read_text(encoding="utf-8") if o.patch_path else ""
        applied, reason = apply_patch(root, patch)
        print(f"  apply {o.child_id}: {applied} ({reason[:60]}) isolated={o.isolated}")

    after = target.read_text()
    print("\n--- shared.py after applying both patches ---")
    print(after)
    print("one() changed:", "return 111" in after)
    print("three() changed:", "return 333" in after)
    print("both survived:", ("return 111" in after) and ("return 333" in after))
    subprocess.run(["git", "status", "--porcelain"], cwd=root)
    subprocess.run(["git", "diff", "--stat"], cwd=root)


if __name__ == "__main__":
    main()
