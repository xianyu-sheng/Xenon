"""Real probe: does a session grant actually remove the second prompt? (#373)"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

from xenon.harness.llm import build_llm_loop_strategy, resolve_model_config
from xenon.harness.runner import run_task
from xenon.kernel.policies import PermissionMode
from xenon.kernel.session_grants import SESSION, GrantBook, grant_from_choice
from xenon.kernel.strategies import LoopProfile

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-flash"

class Approver:
    """Answers 'session' on the first request, then counts how often it is asked."""

    def __init__(self) -> None:
        self.calls = 0
        self.last_scope = "once"
        self.last_reason = ""

    def request(self, view, action, decision):
        self.calls += 1
        cap = getattr(getattr(action, "intent", None), "capability", None)
        print(f"  prompt#{self.calls} for capability={getattr(cap, 'value', cap)}")
        if self.calls == 1:
            self.last_scope = SESSION
            self.last_reason = "user chose: always this session"
            return True
        self.last_scope = "once"
        self.last_reason = "asked again (should not happen)"
        return False

def repo(tag):
    root = Path("/tmp/gr-" + tag + "-" + uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=x@y", "-c", "user.name=x",
                    "commit", "-qm", "init"], cwd=root, check=True)
    return root

def main():
    root = repo("a")
    book = GrantBook(str(root))
    approver = Approver()
    objective = "Run `git log --oneline -n 2` with the shell and report what it printed."

    for turn in (1, 2):
        captured: list = []
        run = run_task(
            objective,
            strategy=build_llm_loop_strategy(resolve_model_config([MODEL]), LoopProfile()),
            workspace=root,
            ledger_dir=Path("/tmp/grl-" + uuid.uuid4().hex[:6]),
            permission=PermissionMode.AUTO_EDIT,
            allow_approval=True,
            approval=approver,
            max_steps=8,
            session_grants=book.active(str(root)),
            on_runtime=captured.append,
        )
        rt = captured[0] if captured else None
        grant = getattr(rt, "last_grant", None)
        print(f"turn {turn}: phase={run.result.phase.value} prompts={approver.calls} "
              f"grant={'yes' if grant else 'no'}")
        if grant is not None:
            book.add(grant)
            print(f"  remembered: {grant.capability.value} scope={grant.scope}")
            from xenon.ledger.store import Ledger
            kinds = [e.kind.value for e in Ledger.open(run.ledger_path, run.task_id).read()]
            print(f"  grant_added in ledger: {'grant_added' in kinds}")
            if grant is not None:
                # the runtime exposes last_grant only after a recorded decision
                pass
    print(f"\ntotal prompts across 2 turns: {approver.calls} (expected 1)")

if __name__ == "__main__":
    main()
