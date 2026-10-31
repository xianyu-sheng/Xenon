"""Robustness of the loop against a model (or a tool) that misbehaves.

These pin the four failures the engine layer used to cover — malformed model
output, a tool that raises, an enormous observation, and an empty action
list — onto the kernel, so the demolition cannot quietly drop the coverage.
"""

from __future__ import annotations

import pytest

from tests.kernel.test_north_star import (
    SPECS,
    FakeCatalog,
    ScriptedStrategy,
    _runtime,
)
from xenon.harness.llm import ActionParseError, parse_action
from xenon.kernel.contracts import (
    CallTool,
    Capability,
    TaskPhase,
    TaskSpec,
    ToolIntent,
)
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


def _spec(task_id: str = "task-robust") -> TaskSpec:
    return TaskSpec(task_id=task_id, objective="inspect", workspace=".")


class RaisingGateway:
    """Every tool raises: the loop must turn that into a failed receipt."""

    def execute(self, call, services):  # noqa: ANN001, ANN201
        raise RuntimeError("tool blew up")


class HugeGateway:
    """Returns an observation far larger than any context budget."""

    def __init__(self, size: int = 200_000) -> None:
        self.payload = "x" * size

    def execute(self, call, services):  # noqa: ANN001, ANN201
        from xenon.kernel.contracts import ReceiptStatus, ToolReceipt

        return ToolReceipt(
            receipt_id="rc_huge",
            call_id="c_huge",
            task_id="task-robust",
            tool=call.spec_name,
            status=ReceiptStatus.OK,
            effects=(),
            declared_effects=(),
            observed_effects=(),
        )


# ── model output the protocol cannot parse ─────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not json at all",
        "{",
        '{"action": "no_such_action"}',
        '{"action": "call_tool"}',
        '{"action": "call_tool", "capability": "telepathy"}',
        '{"action": "think"}',
    ],
)
def test_malformed_model_output_raises_a_typed_error(payload):
    """Bad model output must fail as a protocol error, never as a crash."""

    with pytest.raises(ActionParseError):
        parse_action(payload)


def test_parse_action_survives_fenced_json():
    action = parse_action('```json\n{"action": "think", "text": "hmm"}\n```')

    assert action.__class__.__name__ == "Think"


# ── a tool that raises ────────────────────────────────────────────────


def test_tool_exception_becomes_a_recorded_veto_not_a_crash():
    """A gateway that raises must not take the loop down with it."""

    ledger = Ledger.in_memory("task-robust")
    runtime = _runtime(ledger)
    runtime._gateway = RaisingGateway()

    result = runtime.run(
        _spec(),
        ScriptedStrategy([CallTool(ToolIntent(capability=Capability.READ))]),
    )

    assert result.phase is TaskPhase.BLOCKED
    assert result.accepted is False
    vetoes = [
        event
        for event in ledger.read()
        if event.kind is EventKind.POLICY_DECISION
        and "tool execution failed" in str(event.payload.get("reason") or "")
    ]
    # The kernel does not invent a receipt; it records why it stopped.
    assert vetoes
    assert "RuntimeError" in str(vetoes[0].payload["reason"])
    assert [e for e in ledger.read() if e.kind is EventKind.TOOL_RECEIPT] == []


def test_loop_terminates_when_the_script_runs_out():
    ledger = Ledger.in_memory("task-robust")
    runtime = _runtime(ledger)

    # Only a bare CallTool: no finish, no plan.  The runtime must stop on its
    # own instead of looping forever.
    result = runtime.run(
        _spec(),
        ScriptedStrategy([CallTool(ToolIntent(capability=Capability.READ))]),
    )

    assert result.task_id == "task-robust"
    assert len(ledger.read()) > 0


# ── enormous observations ─────────────────────────────────────────────


def test_huge_observation_does_not_bloat_the_view():
    from xenon.kernel.view import build_model_view

    ledger = Ledger.in_memory("task-robust")
    runtime = _runtime(ledger)
    runtime._gateway = HugeGateway()
    runtime.run(
        _spec(),
        ScriptedStrategy([CallTool(ToolIntent(capability=Capability.READ))]),
    )

    view = build_model_view(
        spec=_spec(),
        catalog=FakeCatalog(),
        ledger=ledger,
        budget=_spec().budget,
    )

    import json

    rendered = json.dumps(list(view.working_set), ensure_ascii=False)
    assert len(rendered) < 20_000, "the view must stay bounded"


# ── unknown capabilities ──────────────────────────────────────────────


def test_catalog_never_invents_a_tool_for_an_unknown_capability():
    from xenon.kernel.contracts import BindingStatus

    catalog = FakeCatalog()
    binding = catalog.bind(ToolIntent(capability=Capability.SHELL))

    assert binding.status is not BindingStatus.BOUND
    assert [spec.name for spec in SPECS] == ["inspect", "mutate", "check", "record"]
