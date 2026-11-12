"""Real probe: the ask-once flow through the RuntimeAdapter — the same layer
``run_kernel_turn`` uses (the REPL's kernel entry).  #373 acceptance:

    1. turn 1 asks the human, who answers "session";
    2. the adapter's book harvests ``runtime.last_grant`` (memory only, §9);
    3. turn 2 feeds ``session_grants`` from the book — zero prompts;
    4. turn 2's ledger has ``policy_decision`` with reason "session grant: …".

Real model, real workspace.  Reproduce:

    PYTHONIOENCODING=utf-8 python docs/evidence/session-grants/adapter_probe.py \\
        deepseek/deepseek-v4-flash
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

from xenon.harness.llm import ModelConfig
from xenon.kernel.policies import PermissionMode
from xenon.kernel.session_grants import GrantBook
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger
from xenon.repl.runtime_adapter import RuntimeAdapter

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-flash"


class Approver:
    """Answers 'session' once, then reports every later ask (should be none)."""

    def __init__(self) -> None:
        self.calls = 0
        self.last_scope = "once"
        self.last_reason = ""

    def request(self, view, action, decision):
        self.calls += 1
        cap = getattr(getattr(action, "intent", None), "capability", None)
        print(f"  prompt#{self.calls} for capability={getattr(cap, 'value', cap)}")
        if self.calls == 1:
            self.last_scope = "session"
            self.last_reason = "session grant: shell"
            return True
        self.last_scope = "once"
        self.last_reason = "asked again (should not happen)"
        return False


def repo(tag: str) -> Path:
    root = Path("/tmp/adp-" + tag + "-" + uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    return root


def main() -> int:
    root = repo("a")
    book = GrantBook(str(root))
    objective = "Run `git log --oneline -n 2` with the shell and report what it printed."

    total_prompts = 0
    for turn in (1, 2):
        approver = Approver()
        adapter = RuntimeAdapter(
            workspace=root,
            model_config=ModelConfig(model_id=MODEL),
            ledger_dir=Path("/tmp/adpl-" + uuid.uuid4().hex[:6]),
            finalize=False,
            permission=PermissionMode.AUTO_EDIT,
            approval=approver,
            allow_approval=True,
            max_steps=8,
        )
        # Exactly what run_kernel_turn wires (#373): the front-end book feeds
        # the next task, and the harvest happens after the run.
        adapter.session_grants = tuple(book.active(str(root)))
        captured: list = []
        run = adapter.run(objective, task_id=f"adapter-turn-{turn}", on_runtime=captured.append)
        runtime = captured[0] if captured else None
        grant = getattr(runtime, "last_grant", None)
        total_prompts += approver.calls
        print(
            f"turn {turn}: phase={run.result.phase.value} "
            f"prompts={approver.calls} grant={'yes' if grant else 'no'}"
        )
        if grant is not None:
            book.add(grant)
            print(f"  remembered: {grant.capability.value} scope={grant.scope} "
                  f"reason={grant.reason!r}")
            ledger = Ledger(run.ledger_path, run.task_id)
            kinds = [event.kind.value for event in ledger.read()]
            print(f"  grant_added in ledger: {'grant_added' in kinds}  (must be False since §9)")
        if turn == 2:
            ledger = Ledger(run.ledger_path, run.task_id)
            grants = [
                event
                for event in ledger.read()
                if event.kind is EventKind.POLICY_DECISION
                and str(event.payload.get("reason") or "").startswith("session grant:")
            ]
            print(
                f"  turn 2 policy_decision reasons: "
                f"{[g.payload.get('reason') for g in grants]}"
            )

    print(f"\ntotal prompts across 2 turns: {total_prompts} (expected 1)")
    if total_prompts != 1:
        print("FAIL: the second task asked again — the grant did not carry over.")
        return 1
    print("PASS: one prompt, two tasks, grant carried by the book.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
