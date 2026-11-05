"""Plan rounds: an incomplete audit starts another round, then discloses.

The turn-level contract (#423): execution is not blocked on the first gap;
the runtime spends up to ``max_plan_rounds`` rounds on it, and only then
returns the answer with the remaining gaps visible.
"""

from __future__ import annotations

from pathlib import Path

from xenon.catalog.catalog import build_catalog
from xenon.catalog.gateway import LegacyToolGateway
from xenon.harness.plan import parse_plan
from xenon.kernel.contracts import (
    Budget,
    Finish,
    Obligation,
    PropositionKind,
    TaskPhase,
    TaskSpec,
)
from xenon.kernel.controller import VerificationController
from xenon.kernel.runtime import LoopRuntime
from xenon.kernel.strategies import LoopProfile, LoopStrategy
from xenon.ledger.store import Ledger


def test_three_rounds_then_deliver_with_gaps(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 0\n", encoding="utf-8")

    rounds = {"planner": 0}

    def planner(view):  # noqa: ANN001 - protocol shape
        rounds["planner"] += 1
        return parse_plan(
            {
                "objective": "edit a.py",
                "steps": [
                    {
                        "id": 1,
                        "kind": "edit",
                        "capability": "edit",
                        "params": {
                            "files": [
                                {
                                    "path": "a.py",
                                    "content": f"x = {rounds['planner']}\n",
                                }
                            ]
                        },
                        "expects": [
                            {
                                "id": "e1",
                                "kind": "file_changed",
                                "target": "a.py",
                            }
                        ],
                    }
                ],
            }
        ).proposal

    strategy = LoopStrategy(
        LoopProfile(plan=True, step_scope=True, follow_plan=True),
        actor=lambda view: Finish("done"),
        planner=planner,
    )
    catalog = build_catalog("core")
    ledger = Ledger.in_memory("rounds")
    runtime = LoopRuntime(
        catalog=catalog,
        gateway=LegacyToolGateway(
            specs=catalog.specs(), workspace=tmp_path
        ),
        controller=VerificationController(),
        ledger=ledger,
        finalize=None,
    )
    spec = TaskSpec(
        task_id="rounds",
        objective="change a.py and b.py",
        workspace=str(tmp_path),
        obligations=(
            Obligation(
                id="o-a",
                kind=PropositionKind.FILE_CHANGED,
                target="a.py",
            ),
            Obligation(
                id="o-b",
                kind=PropositionKind.FILE_CHANGED,
                target="b.py",
            ),
        ),
        budget=Budget(max_plan_rounds=3, max_audit_rounds=1),
    )

    result = runtime.run(spec, strategy)

    assert rounds["planner"] == 3, "the runtime must re-plan, not finish early"
    assert result.metrics["plan_rounds"] == 3.0
    assert result.phase is TaskPhase.BLOCKED
    # The answer survives; the unmet b.py gap is disclosed next to it.
    assert result.answer == "done"
    assert any(item.obligation.id == "o-b" for item in result.debt)
    decisions = [
        event.payload.get("decision")
        for event in ledger.read()
        if event.kind.value == "control_decision"
    ]
    assert decisions.count("start_next_plan_round") == 2
