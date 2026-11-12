"""Real fold probe: dual-summary compaction (design doc §13 decision 1 / defect 4).

A real API run with a deliberately small **effective** context window so the
working set genuinely overflows and folds.  At the fold the runtime must:

1. fold dropped events into the deterministic ``elided`` digest — the fact
   backbone (seq/status/tool/why);
2. ask the real model for a continuation narrative and append one
   ``CONTEXT_SUMMARY`` event, then rebuild the current view so the narrative is
   visible this turn;
3. leave the ``system + tools`` prefix byte-identical — the fold appends at the
   tail, so the provider's cached prefix is unaffected.

This is not a headless double: every model call goes to the real endpoint and
the narrative is the model's own output, audited here for faithfulness.

Reproduce:

    PYTHONIOENCODING=utf-8 python docs/evidence/dual-summary/real_fold_probe.py \
        deepseek/deepseek-chat
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

from xenon.harness.llm import ModelConfig, build_llm_loop_strategy
from xenon.harness.runner import _bind_accounting, _runtime_bundle
from xenon.harness.summarizer import LLMSummarizer
from xenon.kernel.contracts import Budget, TaskSpec
from xenon.kernel.policies import PermissionMode
from xenon.kernel.strategies import LoopProfile
from xenon.ledger.events import EventKind

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-chat"

#: Small enough that a handful of read receipts overflow and fold; the real
#: model window is far larger, but the fold sizes against this budget.
EFFECTIVE_WINDOW = 2200


def workspace() -> Path:
    root = Path("/tmp/ds-" + uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    for index in range(1, 9):
        body = "\n".join(
            f"def helper_{index}_{line}(value):"
            f"\n    # module {index}, line {line}: a distinct fact"
            f"\n    return value + {index * line}"
            for line in range(1, 9)
        )
        (root / f"mod_{index}.py").write_text(body + "\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    return root


def main() -> int:
    root = workspace()
    ledger_dir = Path("/tmp/ds-ledger-" + uuid.uuid4().hex[:6])
    config = ModelConfig(model_id=MODEL)
    summarizer = LLMSummarizer(config)

    root_path, ledger_path, ledger, runtime = _runtime_bundle(
        workspace=root,
        ledger_dir=ledger_dir,
        task_id="dual-summary",
        permission=PermissionMode.BYPASS,
        finalize=False,
        sink=None,
        approval=None,
        allow_approval=False,
        approval_ttl_seconds=60.0,
        catalog_profile="core",
        gateway_context=None,
        extra_specs=(),
        tool_runtime=None,
        audit_provider=None,
        summary_provider=summarizer,
    )

    strategy = build_llm_loop_strategy(config, LoopProfile())
    _bind_accounting(strategy, runtime)

    objective = (
        "Read each of the eight files mod_1.py through mod_8.py one at a time, "
        "in order, using read. After reading all eight, report what helper "
        "functions exist. Do not skip any file."
    )
    spec = TaskSpec(
        task_id="dual-summary",
        objective=objective,
        workspace=str(root_path),
        budget=Budget(max_steps=30, context_window=EFFECTIVE_WINDOW),
    )
    result = runtime.run(spec, strategy)

    events = list(ledger.read())
    compacted = [e for e in events if e.kind is EventKind.CONTEXT_COMPACTED]
    summaries = [e for e in events if e.kind is EventKind.CONTEXT_SUMMARY]

    print(f"phase={result.phase.value} steps={int(result.metrics['steps'])}")
    print(f"CONTEXT_COMPACTED events: {len(compacted)}")
    for event in compacted:
        seqs = event.payload["seqs"]
        print(f"  folded seqs {seqs[0]}..{seqs[-1]} ({event.payload['count']})")
    print(f"CONTEXT_SUMMARY events: {len(summaries)}")
    for event in summaries:
        print(f"  model={event.payload['model']}")
        print("  --- narrative ---")
        print(event.payload["summary"])
        print("  -----------------")

    ok = bool(compacted) and bool(summaries)
    print("PASS: real fold produced both the deterministic digest and LLM narrative"
          if ok else "FAIL: the fold did not produce both halves")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
