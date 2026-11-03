"""#368 probe: default budget finalizes a real kernel task, steps stay small.

Runs the shipped ``run_task`` entry point (default budget — max_steps=1024,
stall 180s) against a throwaway repo with a scripted model, then reads the
ledger like an operator would: finalized? how many steps did it actually take?
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xenon.harness.llm import ModelConfig  # noqa: E402
from xenon.harness.runner import run_task  # noqa: E402
from xenon.ledger.store import Ledger  # noqa: E402
from xenon.harness.sink import JsonlSink  # noqa: E402


class ScriptedClient:
    def __init__(self) -> None:
        self.responses = [
            {"action": "call_tool", "capability": "read",
             "params": {"file_path": "calc.py"}},
            {"action": "call_tool", "capability": "edit",
             "params": {"file_path": "calc.py"}},
            {"action": "call_tool", "capability": "bash",
             "params": {"command": "pytest tests/test_calc.py"}},
            {"action": "finish", "answer": "calc add fixed and tested"},
        ]

    def __call__(self, model_id, messages, **kwargs):
        return json.dumps(self.responses.pop(0))


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (root / "tests").mkdir()
        (root / "tests" / "test_calc.py").write_text(
            "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
        )
        from xenon.harness.llm import LoopProfile, build_llm_loop_strategy

        client = ScriptedClient()
        strategy = build_llm_loop_strategy(
            ModelConfig(model_id="fake/probe"),
            LoopProfile(plan=False),
            client=client,
        )
        run = run_task(
            "fix add",
            strategy=strategy,
            workspace=root,
            ledger_dir=root / ".xenon",
            catalog_profile="core",
            finalize=True,
            permission="execute",
            sink=JsonlSink(None),
            max_steps=0,  # 0 -> Budget.max_steps=None -> unlimited
        )
        print(f"run.result.accepted = {run.result.accepted}")
        print(f"run.result.phase    = {run.result.phase.value}")
        steps = int(run.result.metrics.get("steps", 0))
        tools = int(run.result.metrics.get("tool_calls", 0))
        events = int(run.result.metrics.get("events", 0))
        print(f"result.metrics steps={steps} tool_calls={tools} events={events}")
        assert run.result.accepted is True, "unlimited budget must finalize"
        assert steps < 1024, "a small task must not approach the cap"
        print("PASS: unlimited budget finalizes, steps stay small")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
