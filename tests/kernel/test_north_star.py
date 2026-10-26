"""North-star contracts for the single-loop kernel.

These tests are the target, not the current behavior.  They fail with
``ImportError`` until the runtime lands in #196; after that they must pass
without modification except for the ``xfail`` marker removal.

The point is to keep the contract honest while the implementation is split into
#193–#204.  If the kernel starts branching on concrete tool names, these tests
and ``scripts/check_kernel_purity.py`` both fail.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from xenon.kernel.contracts import (
    Abort,
    BindingStatus,
    CallTool,
    Capability,
    Claim,
    ClaimSource,
    ControlDecision,
    ControlDecisionKind,
    EffectKind,
    Finish,
    FsWrite,
    PropositionKind,
    ReadFact,
    ReceiptStatus,
    Risk,
    StepContract,
    StepKind,
    TaskSpec,
    TestRun,
    ToolBinding,
    ToolCall,
    ToolIntent,
    ToolReceipt,
    ToolSpec,
    VcsCommit,
    Verdict,
    VerdictStatus,
    VerificationStage,
)

pytestmark = pytest.mark.kernel_contract

XFAIL = pytest.mark.xfail(
    raises=ImportError,
    strict=False,
    reason="LoopRuntime lands in #196; this test is the north-star contract",
)

SPECS = (
    ToolSpec(
        name="inspect",
        pack="fake",
        capabilities=frozenset({Capability.READ, Capability.SEARCH}),
        effects=frozenset({EffectKind.READ}),
        risk=Risk.INFO,
    ),
    ToolSpec(
        name="mutate",
        pack="fake",
        capabilities=frozenset({Capability.EDIT}),
        effects=frozenset({EffectKind.FS_WRITE}),
        risk=Risk.WRITE,
    ),
    ToolSpec(
        name="check",
        pack="fake",
        capabilities=frozenset({Capability.VERIFY}),
        effects=frozenset({EffectKind.TEST_RUN}),
        risk=Risk.INFO,
    ),
    ToolSpec(
        name="record",
        pack="fake",
        capabilities=frozenset({Capability.VCS}),
        effects=frozenset({EffectKind.VCS_COMMIT}),
        risk=Risk.SENSITIVE,
    ),
)


class FakeCatalog:
    def specs(self, profile: str = "core") -> tuple[ToolSpec, ...]:
        return SPECS

    def bind(self, intent: ToolIntent) -> ToolBinding:
        matches = [spec for spec in SPECS if intent.capability in spec.capabilities]
        if len(matches) == 1:
            return ToolBinding(
                status=BindingStatus.BOUND,
                call=ToolCall(spec_name=matches[0].name, params=dict(intent.params)),
            )
        if not matches:
            return ToolBinding(
                status=BindingStatus.UNRESOLVED,
                reason=f"no tool for {intent.capability.value}",
            )
        return ToolBinding(
            status=BindingStatus.AMBIGUOUS,
            candidates=tuple(spec.name for spec in matches),
        )


class FakeGateway:
    def execute(self, call: ToolCall, services: Any) -> ToolReceipt:
        effects = {
            "inspect": (ReadFact(path="calc/add.py", content_hash="h"),),
            "mutate": (FsWrite(paths=("calc/add.py",), hashes={"calc/add.py": "h2"}),),
            "check": (
                TestRun(
                    target="tests/test_calc.py",
                    tests_run=1,
                    passed=1,
                    exit_code=0,
                ),
            ),
            "record": (VcsCommit(sha="abc1234", files=("calc/add.py",)),),
        }[call.spec_name]
        return ToolReceipt(
            receipt_id=f"receipt-{call.spec_name}",
            call_id=f"call-{call.spec_name}",
            task_id="task-1",
            tool=call.spec_name,
            status=ReceiptStatus.OK,
            effects=effects,
            declared_effects=tuple(item.kind for item in effects),
            observed_effects=tuple(item.kind for item in effects),
        )


class FakeLedger:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def append(self, event: Any) -> int:
        self.events.append(event)
        return len(self.events)

    def read(
        self, kind: str | None = None, up_to_seq: int | None = None
    ) -> tuple[Any, ...]:
        limit = up_to_seq if up_to_seq is not None else len(self.events)
        items = self.events[:limit]
        if kind is None:
            return tuple(items)
        return tuple(item for item in items if getattr(item, "kind", None) == kind)

    def watermark(self) -> int:
        return len(self.events)

    def projection(
        self, name: str, up_to_seq: int | None = None
    ) -> dict[str, Any]:
        limit = up_to_seq if up_to_seq is not None else len(self.events)
        payload = json.dumps(
            [repr(item) for item in self.events[:limit]], sort_keys=True
        )
        return {
            "name": name,
            "hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "events": limit,
        }

    def verify(self) -> bool:
        return True

    def effects(self) -> tuple[Any, ...]:
        result: list[Any] = []
        for event in self.events:
            result.extend(getattr(event, "effects", ()) or ())
        return tuple(result)


class StrictFakeController:
    """A tiny stand-in: the real controller lands in #199."""

    def reconcile(self, step: StepContract | None, receipt: ToolReceipt) -> Verdict:
        obligation_id = step.expects[0].id if step and step.expects else "receipt"
        status = (
            VerdictStatus.SATISFIED
            if receipt.status is ReceiptStatus.OK
            else VerdictStatus.VIOLATED
        )
        return Verdict(
            obligation_id=obligation_id,
            stage=VerificationStage.V0,
            status=status,
        )

    def close_step(
        self, step: StepContract, ledger: FakeLedger
    ) -> tuple[Verdict, ...]:
        return tuple(
            Verdict(
                obligation_id=item.id,
                stage=VerificationStage.V1,
                status=VerdictStatus.SATISFIED,
            )
            for item in step.expects
        )

    def audit(
        self, claim: Claim | None, ledger: FakeLedger, debt: Any
    ) -> Verdict:
        return Verdict(
            obligation_id=claim.id if claim else "audit",
            stage=VerificationStage.V2,
            status=VerdictStatus.SATISFIED if not debt else VerdictStatus.VIOLATED,
        )

    def decide(self, verdicts: Any, debt: Any) -> ControlDecision:
        if debt:
            return ControlDecision(ControlDecisionKind.BLOCK, reason="debt")
        return ControlDecision(ControlDecisionKind.DELIVER)


class ScriptedStrategy:
    def __init__(self, actions: list[Any]) -> None:
        self.actions = list(actions)

    def propose(self, view: Any) -> Any:
        if not self.actions:
            return Abort("script exhausted")
        return self.actions.pop(0)


def _runtime(ledger: FakeLedger):
    from xenon.kernel.runtime import LoopRuntime

    return LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=StrictFakeController(),
        ledger=ledger,
        sink=None,
    )


def _calc_spec() -> TaskSpec:
    return TaskSpec(task_id="task-1", objective="fix add", workspace=".")


@XFAIL
def test_fake_catalog_arbitrary_names() -> None:
    ledger = FakeLedger()
    runtime = _runtime(ledger)
    strategy = ScriptedStrategy(
        [
            CallTool(ToolIntent(capability=Capability.READ)),
            CallTool(ToolIntent(capability=Capability.EDIT)),
            CallTool(ToolIntent(capability=Capability.VERIFY)),
            CallTool(ToolIntent(capability=Capability.VCS)),
            Finish("done"),
        ]
    )

    result = runtime.run(_calc_spec(), strategy)

    assert result.accepted is True
    assert result.finalize_sha == "abc1234"
    tool_names = {
        event.tool for event in ledger.events if hasattr(event, "tool")
    }
    assert {"inspect", "mutate", "check", "record"} <= tool_names


@XFAIL
def test_lying_strategy_cannot_deliver() -> None:
    ledger = FakeLedger()
    runtime = _runtime(ledger)
    lie = Claim(
        id="claim-lie",
        kind=PropositionKind.FILE_CHANGED,
        target="calc/add.py",
        source=ClaimSource.LLM,
    )
    strategy = ScriptedStrategy([Finish("I fixed it", claims=(lie,))])

    result = runtime.run(_calc_spec(), strategy)

    assert result.accepted is False
    assert result.debt, "a claim without a fact must become debt"
    assert not any(
        getattr(event, "tool", None) for event in ledger.events
    ), "no tool should have run"


@XFAIL
def test_replay_is_deterministic() -> None:
    def run_once() -> dict[str, Any]:
        ledger = FakeLedger()
        runtime = _runtime(ledger)
        strategy = ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.READ)),
                CallTool(ToolIntent(capability=Capability.EDIT)),
                Finish("done"),
            ]
        )
        runtime.run(_calc_spec(), strategy)
        return ledger.projection("evidence")

    assert run_once()["hash"] == run_once()["hash"]


def test_fake_catalog_binds_by_capability_not_name() -> None:
    catalog = FakeCatalog()
    binding = catalog.bind(ToolIntent(capability=Capability.EDIT))
    assert binding.status is BindingStatus.BOUND
    assert binding.call is not None
    assert binding.call.spec_name == "mutate"
    assert catalog.bind(ToolIntent(capability=Capability.REMOTE)).status is BindingStatus.UNRESOLVED


def test_step_contract_has_obligations_not_tool_names() -> None:
    step = StepContract(
        id=1,
        kind=StepKind.VERIFY,
        targets=("tests/test_calc.py",),
    )
    assert step.kind is StepKind.VERIFY
    assert step.targets == ("tests/test_calc.py",)
