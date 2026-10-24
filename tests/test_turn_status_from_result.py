"""Non-final engine outcomes must not be published as successful turns."""

from __future__ import annotations

from xenon.engine.result import AgentResult, TerminationReason
from xenon.repl.turn_helpers import turn_status_from_result


def test_loop_result_overrides_gate_pass():
    result = AgentResult(
        "engine looped",
        reason=TerminationReason.LOOP,
        iterations=100,
        tool_calls=56,
    )

    status, reasons = turn_status_from_result(result, "passed")

    assert status == "fused"
    assert any("loop" in reason for reason in reasons)


def test_delivery_incomplete_result_is_degraded():
    result = AgentResult(
        "missing verification",
        reason=TerminationReason.DELIVERY_INCOMPLETE,
        missing_delivery=("verify", "commit"),
    )

    status, reasons = turn_status_from_result(result, "passed")

    assert status == "degraded"
    assert any("verify" in reason for reason in reasons)


def test_final_result_keeps_gate_status():
    result = AgentResult("done", reason=TerminationReason.FINAL)

    assert turn_status_from_result(result, "passed") == ("passed", [])
    assert turn_status_from_result(result, "degraded") == ("degraded", [])


def test_gate_fuse_is_not_downgraded_by_loop_reason():
    result = AgentResult("loop", reason=TerminationReason.LOOP)

    status, _ = turn_status_from_result(result, "fused")

    assert status == "fused"
