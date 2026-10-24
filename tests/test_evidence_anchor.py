"""Optional external anchor for the durable evidence ledger (#175)."""

from __future__ import annotations

import json

from xenon.engine.evidence_runtime import (
    EventKind,
    EvidenceLedger,
    EvidenceSource,
    LifecyclePhase,
)


def _ledger() -> EvidenceLedger:
    ledger = EvidenceLedger("anchor-session")
    ledger.append(
        LifecyclePhase.TASK,
        EventKind.TASK_FACT,
        EvidenceSource.ENGINE,
        {"task_id": "t1"},
    )
    return ledger


def test_anchor_matches_current_head_and_detects_append(tmp_path) -> None:
    ledger = _ledger()
    anchor = tmp_path / "anchor.json"

    head = ledger.write_anchor(anchor)
    assert head == ledger.events[-1].content_hash
    assert ledger.verify_anchor(anchor) is True

    ledger.append(
        LifecyclePhase.EXECUTION,
        EventKind.TOOL_OBSERVATION,
        EvidenceSource.TOOL,
        {"tool": "read_file", "success": True},
    )
    assert ledger.verify_anchor(anchor) is False


def test_anchor_detects_rewritten_head_and_missing_file(tmp_path) -> None:
    ledger = _ledger()
    anchor = tmp_path / "anchor.json"
    ledger.write_anchor(anchor)
    payload = json.loads(anchor.read_text(encoding="utf-8"))
    payload["head_hash"] = "0" * 64
    anchor.write_text(json.dumps(payload), encoding="utf-8")

    assert ledger.verify_anchor(anchor) is False
    assert ledger.verify_anchor(tmp_path / "missing.json") is False
