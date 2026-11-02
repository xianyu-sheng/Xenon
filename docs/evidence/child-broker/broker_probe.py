"""Real probe: can a child verify its work when the parent brokers approval?

Before #356 a child ended ``blocked`` on ``verify requires a grant`` — it had no
terminal, so nobody could approve.  Here the parent supplies a provider and the
child asks through the file broker.

Three scenarios, all with the real model:
  1. parent approves  -> child may run the test and can finish;
  2. parent denies    -> child must NOT proceed, and must say why;
  3. no provider      -> fail closed with a reason.
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
from xenon.replay import children_of, render_children  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-flash"


class Approver:
    def __init__(self, allow: bool, reason: str = "") -> None:
        self.allow = allow
        self.reason = reason
        self.calls: list[dict] = []

    def approve_request(self, request):
        self.calls.append(request)
        return self.allow, self.reason or ("approved" if self.allow else "denied")


def repo(tag: str) -> Path:
    root = Path("/tmp/br-" + tag + "-" + uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    return root


def run(label: str, approval, *, permission=PermissionMode.AUTO_EDIT) -> None:
    parent = f"broker-{label}"
    approver = approval
    outcome = run_child_task(
        # `bash pytest` is a *safe test command* (allowed without approval), so
        # this objective deliberately asks for a command that does need a grant.
        objective=(
            "Run the shell command `git log --oneline -n 3` and report what it "
            "printed. Do not modify any file."
        ),
        workspace=repo(label),
        model=resolve_model_config([MODEL]),
        profile="kernel",
        parent_task_id=parent,
        ledger_dir=Path("/tmp/brl-" + uuid.uuid4().hex[:6]),
        permission=permission,
        max_steps=14,
        timeout=240,
        approval=approver,
    )
    print(f"\n=== {label} ===")
    print(f"phase={outcome.phase} accepted={outcome.accepted} killed={outcome.killed}")
    print(f"debt={list(outcome.debt)}")
    if isinstance(approver, Approver):
        print(f"approval requests seen by parent: {len(approver.calls)}")
        for call in approver.calls[:4]:
            print("   ", json.dumps(call, ensure_ascii=False)[:120])
    rows = children_of(parent, root=Path(outcome.ledger_path).parent)
    print(render_children(rows, parent=parent))


def main() -> None:
    run("allow", Approver(True))
    run("deny", Approver(False, "operator refused: do not touch tests"))
    run("noprovider", None)


if __name__ == "__main__":
    main()
