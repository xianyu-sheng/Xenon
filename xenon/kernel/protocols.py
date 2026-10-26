"""Structural protocols for the kernel roles.

These are typing contracts, not implementations.  The legacy engine must not be
imported here; concrete adapters live outside ``xenon.kernel``.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from xenon.kernel.contracts import (
    Action,
    Claim,
    ControlDecision,
    Debt,
    ModelView,
    PolicyDecision,
    Resolution,
    StepContract,
    TaskResult,
    TaskSpec,
    ToolBinding,
    ToolCall,
    ToolIntent,
    ToolReceipt,
    ToolSpec,
    Verdict,
)


@runtime_checkable
class Strategy(Protocol):
    """Proposes the next action.  Never executes or writes state."""

    def propose(self, view: ModelView) -> Action: ...


@runtime_checkable
class Policy(Protocol):
    """Deterministic veto / approval layer."""

    def check(self, view: ModelView, action: Action) -> PolicyDecision: ...


@runtime_checkable
class ToolCatalog(Protocol):
    """Capability-aware view of the tool surface."""

    def specs(self, profile: str = "core") -> Sequence[ToolSpec]: ...

    def bind(self, intent: ToolIntent) -> ToolBinding:
        """Resolve capability -> tool, or return an explicit unresolved/ambiguous."""
        ...


@runtime_checkable
class RuntimeServices(Protocol):
    """Small runtime surface passed to the gateway (no global context bag)."""

    @property
    def task_id(self) -> str: ...

    @property
    def workspace(self) -> str: ...

    @property
    def current_step_id(self) -> str | None: ...

    def is_cancelled(self) -> bool: ...


@runtime_checkable
class ToolGateway(Protocol):
    """The only side-effect entrance."""

    def execute(self, call: ToolCall, services: RuntimeServices) -> ToolReceipt: ...


@runtime_checkable
class VerificationController(Protocol):
    """Single verification authority (V0/V1/V2)."""

    def reconcile(
        self, step: StepContract | None, receipt: ToolReceipt
    ) -> Verdict: ...

    def close_step(
        self, step: StepContract, ledger: Ledger
    ) -> tuple[Verdict, ...]: ...

    def resolve(self, claim: Claim, ledger: Ledger) -> Resolution: ...

    def audit(
        self, claim: Claim | None, ledger: Ledger, debt: Sequence[Debt]
    ) -> Verdict: ...

    def decide(
        self, verdicts: Sequence[Verdict], debt: Sequence[Debt]
    ) -> ControlDecision: ...


@runtime_checkable
class Ledger(Protocol):
    """Append-only event store.  Projections only get a read-only view."""

    def append(self, event: Any) -> int: ...

    def read(
        self, kind: str | None = None, up_to_seq: int | None = None
    ) -> Sequence[Any]: ...

    def watermark(self) -> int: ...

    def projection(
        self, name: str, up_to_seq: int | None = None
    ) -> Mapping[str, Any]: ...

    def verify(self) -> bool: ...


@runtime_checkable
class EventSink(Protocol):
    """UI / JSONL / test recorder.  Ephemeral, never a source of truth."""

    def emit(self, event: Any) -> None: ...


@runtime_checkable
class Runtime(Protocol):
    """What the REPL and headless entry points depend on."""

    def run(self, spec: TaskSpec, strategy: Strategy) -> TaskResult: ...
