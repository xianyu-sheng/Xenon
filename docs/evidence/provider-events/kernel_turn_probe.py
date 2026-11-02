"""Real probe: the kernel turn reports provider phase end to end (#320).

Uses the real `run_task` entry point with a real model and a real sink, so this
covers the chain `run_task -> strategy -> _call_model -> chat_completion -> sink`
rather than a single function in isolation.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

from xenon.harness.llm import (  # noqa: E402
    build_llm_loop_strategy,
    bind_request_sink,
    resolve_model_config,
)
from xenon.harness.runner import run_task  # noqa: E402
from xenon.kernel.policies import PermissionMode  # noqa: E402
from xenon.kernel.strategies import LoopProfile  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-flash"


def main() -> None:
    root = Path("/tmp/prov-" + uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )

    events: list[dict] = []
    strategy = build_llm_loop_strategy(
        resolve_model_config([MODEL]), LoopProfile()
    )
    bind_request_sink(strategy, events.append)

    run = run_task(
        "Read calc.py and report which function it defines. Change nothing.",
        strategy=strategy,
        workspace=root,
        ledger_dir=Path("/tmp/provl-" + uuid.uuid4().hex[:6]),
        permission=PermissionMode.READ_ONLY,
        max_steps=6,
    )

    print(f"phase={run.result.phase.value} accepted={run.result.accepted}")
    print(f"provider events: {len(events)}")
    for event in events:
        if event["kind"] == "start":
            print(
                f"  start {event['phase']} attempt={event['attempt']}"
                f"/{event['provider_attempts']} streamed={event['streamed']}"
            )
        else:
            print(f"  end   success={event['success']} attempt={event['attempt']}")
    if not events:
        print("  (none — the bug)")

    from xenon.ledger.store import Ledger

    kinds = [e.kind.value for e in Ledger.open(run.ledger_path, run.task_id).read()]
    print(f"ledger kinds unique: {sorted(set(kinds))}")


if __name__ == "__main__":
    main()
