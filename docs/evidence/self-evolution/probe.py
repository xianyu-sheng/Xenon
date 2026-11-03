"""Real probe: self-evolution slice 1+2 (docs/design/self-evolution.md).

A real task where the human denies the first approval: the runtime must write
a LESSON_PROPOSED event (deterministic template, no LLM) whose fingerprint the
LessonStore can later match.  Storage/application (slices 3-4) are next.

Reproduce:

    PYTHONIOENCODING=utf-8 python docs/evidence/self-evolution/probe.py \\
        deepseek/deepseek-v4-flash
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

from xenon.harness.llm import ModelConfig
from xenon.kernel.lessons import lesson_fingerprint
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger
from xenon.repl.runtime_adapter import RuntimeAdapter

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-flash"


class _DenyOnce:
    """Deny the first approval, then approve — a real human's first reaction."""

    def __init__(self) -> None:
        self.calls = 0
        self.last_scope = ""
        self.last_reason = ""

    def request(self, view, action, decision) -> bool:
        self.calls += 1
        if self.calls == 1:
            self.last_reason = "user: 先别跑"
            return False
        self.last_reason = "session grant: shell"
        return True


def repo() -> Path:
    root = Path("/tmp/se-" + uuid.uuid4().hex[:6])
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
    approver = _DenyOnce()
    adapter = RuntimeAdapter(
        workspace=root,
        model_config=ModelConfig(model_id=MODEL),
        ledger_dir=Path("/tmp/sel-" + uuid.uuid4().hex[:6]),
        finalize=False,
        approval=approver,
        allow_approval=True,
        max_steps=10,
    )
    run = adapter.run(
        "Run `git log --oneline -n 2` with the shell and report it.",
        task_id="self-evolve",
    )
    ledger = Ledger(run.ledger_path, "self-evolve")
    events = list(ledger.read())

    denials = [
        event for event in events
        if event.kind is EventKind.USER_APPROVAL and not event.payload.get("approved")
    ]
    proposals = [
        event for event in events if event.kind is EventKind.LESSON_PROPOSED
    ]
    print(f"task phase: {run.result.phase.value}  accepted: {run.result.accepted}")
    print(f"denied approvals: {len(denials)}; lesson proposals: {len(proposals)}")
    for proposal in proposals:
        payload = proposal.payload
        print(f"  {payload['kind']} | trigger={payload['trigger_event']} "
              f"| fp={payload['fingerprint'][:10]}…")
        print(f"    action: {payload['action_text'][:80]}")
    if not proposals:
        print("FAIL: no lesson was proposed from the denial")
        return 1
    fp = proposals[0].payload["fingerprint"]
    expect = lesson_fingerprint(
        "user_approval",
        {"approved": False, "tool": "shell", "reason": "user: 先别跑"},
    )
    print(f"fingerprint matches proposer: {fp == expect}")
    print("PASS: a denial proposes a preference lesson, deterministically")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
