"""#421: the audit tool itself must be able to say "missing".

The point of `scripts/audit_mechanisms.py` is to stop accepting "done" from code
reading; if it cannot report a missing mechanism, it is decoration.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _audit_module():
    spec = importlib.util.spec_from_file_location(
        "audit_mechanisms", ROOT / "scripts" / "audit_mechanisms.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # A module that is not in sys.modules cannot own a dataclass.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _ledger(path: Path, events: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n",
        encoding="utf-8",
    )
    return path


def _verdict(stage: str) -> dict:
    return {"kind": "step_verdict", "payload": {"stage": stage, "status": "satisfied"}}


def test_a_ledger_with_only_v0_reports_the_missing_levels(tmp_path: Path) -> None:
    ledger = _ledger(
        tmp_path / "only-v0.jsonl",
        [
            {
                "kind": "tool_receipt",
                "payload": {
                    "receipt": {
                        "status": "ok",
                        "effects": [
                            {"type": "fs_write", "paths": ["calc.py"], "changed": True}
                        ],
                    }
                },
            },
            _verdict("v0"),
            {"kind": "audit_reported", "payload": {"verdicts": []}},
        ],
    )

    rows = {name: (detail, state) for name, detail, state in _audit_module().audit(ledger)}

    detail, state = rows["三级齐备 V0/V1/V2"]
    assert state == "miss", detail
    assert "v1" in detail and "v2" in detail


def test_a_failure_without_recorded_advice_is_reported(tmp_path: Path) -> None:
    ledger = _ledger(
        tmp_path / "no-teaching.jsonl",
        [
            {"kind": "tool_receipt", "payload": {"receipt": {"status": "failed"}}},
        ],
    )

    rows = {name: (detail, state) for name, detail, state in _audit_module().audit(ledger)}

    detail, state = rows["失败即教学（可观测）"]
    assert state == "miss", detail
    assert "0 条教学提示" in detail


def test_an_all_green_ledger_passes(tmp_path: Path) -> None:
    ledger = _ledger(
        tmp_path / "green.jsonl",
        [
            {"kind": "task_started", "payload": {"objective": "x"}},
            {"kind": "tool_receipt", "payload": {"receipt": {"status": "ok"}}},
            _verdict("v0"),
            _verdict("v1"),
            _verdict("v2"),
            {"kind": "audit_reported", "payload": {"verdicts": []}},
            {"kind": "finalize_started", "payload": {"verified_paths": ["a.py"]}},
            {"kind": "model_usage", "payload": {"prompt_tokens": 10}},
        ],
    )

    rows = _audit_module().audit(ledger)

    # A hand-written fixture has no hash chain, so the integrity check is
    # expected to fail here; it is exercised against real ledgers instead.
    judged = [row for row in rows if row[0] != "账本链完整性"]
    assert all(state != "miss" for _, _, state in judged), judged
