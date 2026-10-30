"""Run one real-model reflection turn and record the ledger as evidence.

Not a test: it drives the real react + reflect loop against a real
provider so the reflection slice has a reproducible artifact.
"""

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
OUT = ROOT / "docs" / "evidence" / "reflection-parity"
WS = ROOT / "build" / "tmp-reflect" / "ws"
TASK = (
    "In calc.py add a subtract(a, b) function that returns a - b. "
    "Then add a test for it in test_calc.py and run the tests."
)


def _model_config() -> ModelConfig:
    """Build a ModelConfig from the REPL's provider registry + credentials."""

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
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if not WS.is_dir():
        print(f"missing workspace: {WS}", file=sys.stderr)
        return 2
    config = _model_config()
    strategy = build_llm_loop_strategy(
        config, LoopProfile(reflect_rounds=1, reflect_calls=1)
    )
    run = run_task(
        TASK,
        strategy=strategy,
        workspace=WS,
        ledger_dir=OUT / "ledger",
        task_id="reflection-parity",
        permission=PermissionMode.AUTO_EDIT,
        finalize=True,
        max_steps=12,
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
