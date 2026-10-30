"""Run one real-model plan-react turn and record the ledger as evidence."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from xenon.harness.llm import ModelConfig, build_llm_loop_strategy
from xenon.harness.runner import result_json, run_task
from xenon.kernel.policies import PermissionMode
from xenon.kernel.strategies import LoopProfile
from xenon.repl.provider_registry import get_provider

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "evidence" / "plan-react-parity"
WS = ROOT / "build" / "tmp-plan-react" / "ws"

TASK = (
    "In calc.py add a multiply(a, b) function, and also a power(a, b) function. "
    "Add one test per function in test_calc.py, then run the tests."
)


def _model_config() -> ModelConfig:
    provider_name = os.environ.get("XENON_EVIDENCE_PROVIDER", "deepseek")
    provider = get_provider(provider_name)
    if provider is None:
        raise SystemExit(f"unknown provider: {provider_name}")
    creds = yaml.safe_load(
        Path.home().joinpath(".xenon", "credentials.yaml").read_text(
            encoding="utf-8"
        )
    )
    api_key = str(creds.get(provider_name) or os.environ.get(provider.env_key, ""))
    if not api_key:
        raise SystemExit(f"no api key for {provider_name}")
    model = os.environ.get("XENON_EVIDENCE_MODEL") or provider.models[0]
    return ModelConfig(
        model_id=f"{provider_name}/{model}",
        base_url=provider.base_url,
        api_key=api_key,
    )


def main() -> int:
    if not WS.is_dir():
        print(f"missing workspace: {WS}", file=sys.stderr)
        return 2
    if OUT.exists():
        shutil.rmtree(OUT)
    run = run_task(
        TASK,
        strategy=build_llm_loop_strategy(
            _model_config(), LoopProfile(plan=True, step_scope=True, step_calls=6)
        ),
        workspace=WS,
        ledger_dir=OUT / "ledger",
        task_id="plan-react-parity",
        permission=PermissionMode.AUTO_EDIT,
        finalize=True,
        max_steps=16,
    )
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(run.ledger_path, OUT / "ledger.jsonl")
    anchor = run.ledger_path.with_suffix(".jsonl.anchor")
    if anchor.exists():
        shutil.copyfile(anchor, OUT / "ledger.jsonl.anchor")
    (OUT / "result.json").write_text(
        result_json(run.result) + "\n", encoding="utf-8"
    )
    diff = subprocess.run(
        ["git", "show", "--stat", "--oneline", "HEAD"],
        cwd=WS,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    (OUT / "commit.txt").write_text(diff.stdout or "", encoding="utf-8")
    print(
        json.dumps(
            {
                "accepted": run.result.accepted,
                "phase": run.result.phase.value,
                "finalize_sha": run.result.finalize_sha,
                "debt": [item.reason for item in run.result.debt],
                "metrics": dict(run.result.metrics),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if run.result.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
