"""Real-task probe: does an abandoned child keep mutating the workspace?

Not a unit test.  Uses the real model through ``run_child_task`` and watches the
filesystem *after* the call has returned a timeout, which is the moment the
parent believes the child is gone.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

from xenon.harness.child import run_child_task  # noqa: E402
from xenon.harness.llm import ModelConfig, resolve_model_config  # noqa: E402
from xenon.kernel.policies import PermissionMode  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-flash"


def snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".git" not in path.parts:
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()[:12]
    return out


def main() -> None:
    root = Path("/tmp/cp-" + uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    (root / "notes").mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "notes" / "todo.txt").write_text("nothing yet\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )

    config: ModelConfig = resolve_model_config([MODEL])
    before = snapshot(root)
    print("workspace:", root)
    print("before:", before)

    started = time.time()
    outcome = run_child_task(
        objective=(
            "Add a sub(a, b) function to calc.py that returns a - b. "
            "Then write a file finished.txt containing the word done."
        ),
        workspace=root,
        model=config,
        profile="kernel",
        parent_task_id="probe",
        ledger_dir=Path("/tmp/cpl-" + uuid.uuid4().hex[:6]),
        permission=PermissionMode.BYPASS,
        max_steps=12,
        timeout=6.0,
    )
    returned = time.time()
    print(
        f"returned after {returned - started:.1f}s: "
        f"phase={outcome.phase} err={outcome.error}"
    )

    at_return = snapshot(root)
    print("at return:", at_return)

    changed_later: list[str] = []
    for i in range(12):
        time.sleep(5)
        now = snapshot(root)
        new = {k: v for k, v in now.items() if at_return.get(k) != v}
        if new:
            changed_later.append(f"+{5 * (i + 1)}s {sorted(new)}")
    print("changed after return:", changed_later or "none")
    print("final:", snapshot(root))
    subprocess.run(["git", "status", "--porcelain"], cwd=root)


if __name__ == "__main__":
    main()
