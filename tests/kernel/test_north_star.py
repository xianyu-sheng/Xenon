"""North-star contracts for the single-loop kernel.

The runtime (#196) must pass the arbitrary-name and replay contracts.  The
lying-model contract needs the real claim resolver (#199), so it stays xfail
with an explicit reason.

If the kernel starts branching on concrete tool names, these tests and
``scripts/check_kernel_purity.py`` both fail.
"""

from __future__ import annotations

from typing import Any

import pytest

from xenon.kernel.contracts import (
    Abort,
    AskUser,
    BindingStatus,
    Budget,
    CallTool,
    Capability,
    Claim,
    ClaimSource,
    EffectKind,
    EvidenceStatus,
    Finish,
    FsWrite,
    PolicyDecision,
    PolicyDecisionKind,
    PropositionKind,
    ReadFact,
    ReceiptStatus,
    Risk,
    StepContract,
    StepKind,
    TaskMode,
    TaskPhase,
    TaskSpec,
    TestRun,
    Think,
    ToolBinding,
    ToolCall,
    ToolIntent,
    ToolReceipt,
    ToolSpec,
    VcsCommit,
)
from xenon.kernel.controller import VerificationController
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger

pytestmark = pytest.mark.kernel_contract

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


class ScriptedStrategy:
    def __init__(self, actions: list[Any]) -> None:
        self.actions = list(actions)

    def propose(self, view: Any) -> Any:
        if not self.actions:
            return Abort("script exhausted")
        return self.actions.pop(0)


def _repeating(*actions: Any) -> Any:
    """Scripted double that keeps answering after the script runs out."""

    remaining = list(actions)

    def propose(view: Any) -> Any:
        if remaining:
            return remaining.pop(0)
        return Finish("done")

    return propose


class VetoPolicy:
    def check(self, view: Any, action: Any) -> PolicyDecision:
        return PolicyDecision(PolicyDecisionKind.VETO, "not allowed")


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


def _runtime(ledger: Ledger, *, policies: Any = (), sink: Any = None):
    from xenon.kernel.runtime import LoopRuntime

    return LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
        sink=sink,
        policies=policies,
    )


def _calc_spec() -> TaskSpec:
    return TaskSpec(task_id="task-1", objective="fix add", workspace=".")


def test_fake_catalog_arbitrary_names() -> None:
    ledger = Ledger.in_memory("task-1")
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
        event.payload["receipt"]["tool"]
        for event in ledger.read(EventKind.TOOL_RECEIPT)
    }
    assert {"inspect", "mutate", "check", "record"} <= tool_names


def test_lying_strategy_cannot_deliver() -> None:
    ledger = Ledger.in_memory("task-1")
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
    assert not ledger.read(EventKind.TOOL_RECEIPT), "no tool should have run"


def test_reflection_cannot_launder_an_unverified_claim() -> None:
    """A reflector's "looks done" is a proposal, never a verdict."""

    from xenon.kernel.strategies import ActFirst, ReflectFirst

    ledger = Ledger.in_memory("task-1")
    runtime = _runtime(ledger)
    lie = Claim(
        id="claim-lie",
        kind=PropositionKind.FILE_CHANGED,
        target="calc/add.py",
        source=ClaimSource.LLM,
    )
    strategy = ReflectFirst(
        ActFirst(
            _repeating(
                CallTool(ToolIntent(capability=Capability.READ)),
                Finish("I fixed it", claims=(lie,)),
            )
        ),
        lambda view: Finish("I reviewed it; the claim is fine."),
    )

    result = runtime.run(_calc_spec(), strategy)

    assert result.accepted is False
    assert result.debt, "反思不能关闭未绑定的义务"
    assert result.phase is not TaskPhase.DELIVERED


def test_reflection_can_add_a_corrective_tool_call() -> None:
    from xenon.kernel.strategies import ActFirst, ReflectFirst

    ledger = Ledger.in_memory("task-1")
    runtime = _runtime(ledger)
    strategy = ReflectFirst(
        ActFirst(
            _repeating(
                CallTool(ToolIntent(capability=Capability.READ)),
            )
        ),
        lambda view: CallTool(ToolIntent(capability=Capability.EDIT)),
    )

    result = runtime.run(_calc_spec(), strategy)

    assert len(ledger.read(EventKind.TOOL_RECEIPT)) == 2
    assert result.accepted is True


def test_replay_is_deterministic() -> None:
    def run_once() -> str:
        ledger = Ledger.in_memory("task-1")
        runtime = _runtime(ledger)
        strategy = ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.READ)),
                CallTool(ToolIntent(capability=Capability.EDIT)),
                Finish("done"),
            ]
        )
        runtime.run(_calc_spec(), strategy)
        return ledger.projection_hash("evidence")

    assert run_once() == run_once()


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


def test_budget_stops_the_loop() -> None:
    ledger = Ledger.in_memory("task-1")
    runtime = _runtime(ledger)
    spec = TaskSpec(
        task_id="task-1",
        objective="x",
        workspace=".",
        budget=Budget(max_steps=1),
    )
    result = runtime.run(spec, ScriptedStrategy([Think("plan"), Finish("done")]))
    assert result.accepted is False
    assert result.phase is TaskPhase.BLOCKED
    assert result.metrics["steps"] == 1


def test_cancel_before_run_aborts() -> None:
    ledger = Ledger.in_memory("task-1")
    runtime = _runtime(ledger)
    runtime.request_cancel()
    result = runtime.run(_calc_spec(), ScriptedStrategy([Finish("done")]))
    assert result.accepted is False
    assert result.phase is TaskPhase.ABORTED


def test_policy_veto_blocks_before_execution() -> None:
    ledger = Ledger.in_memory("task-1")
    runtime = _runtime(ledger, policies=[VetoPolicy()])
    result = runtime.run(_calc_spec(), ScriptedStrategy([Finish("done")]))
    assert result.accepted is False
    assert result.phase is TaskPhase.BLOCKED
    assert any(
        event.kind is EventKind.POLICY_DECISION for event in ledger.read()
    )


def test_chat_is_accepted_but_never_verified() -> None:
    ledger = Ledger.in_memory("task-1")
    runtime = _runtime(ledger)
    spec = TaskSpec(
        task_id="task-1",
        objective="hello",
        workspace=".",
        mode=TaskMode.CHAT,
    )
    result = runtime.run(spec, ScriptedStrategy([Finish("hi")]))
    assert result.accepted is True
    assert result.evidence_status is EvidenceStatus.UNVERIFIED


def test_ask_user_blocks_instead_of_guessing() -> None:
    ledger = Ledger.in_memory("task-1")
    runtime = _runtime(ledger)
    result = runtime.run(_calc_spec(), ScriptedStrategy([AskUser("which file?")]))
    assert result.accepted is False
    assert result.phase is TaskPhase.BLOCKED
    assert result.evidence_status is EvidenceStatus.ABSTAINED
    assert any(event.kind is EventKind.USER_QUESTION for event in ledger.read())


def test_task_id_mismatch_is_rejected() -> None:
    runtime = _runtime(Ledger.in_memory("other"))
    with pytest.raises(ValueError, match="does not match"):
        runtime.run(_calc_spec(), ScriptedStrategy([Finish("done")]))


def test_event_sink_receives_events() -> None:
    sink = RecordingSink()
    runtime = _runtime(Ledger.in_memory("task-1"), sink=sink)
    runtime.run(_calc_spec(), ScriptedStrategy([Finish("done")]))
    assert sink.events
    assert sink.events[0].kind is EventKind.TASK_STARTED
