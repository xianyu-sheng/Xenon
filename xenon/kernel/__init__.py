"""Xenon kernel contracts.

The kernel is the single-loop, evidence-first harness described in
``docs/adr/0001-single-loop-kernel.md``.  It owns contracts, the ledger-backed
runtime, verification and strategy seams; projections live in ``xenon.ledger``.

Exports are lazy (PEP 562).  An eager re-export here created a real import
cycle: ``xenon.ledger.events`` imports ``xenon.kernel.contracts``, importing
the ``xenon.kernel`` package ran this file, which imported ``audit`` →
``controller`` → back into the half-initialised ``ledger.events``.  Anything
that touched the ledger before the kernel (a report, a replay tool, a CI job)
failed with "partially initialized module".  Keeping this file import-free
breaks the cycle without changing the public API.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_CONTRACTS = frozenset(
    {
        "SCHEMA_VERSION",
        "Action",
        "ActionKind",
        "AskUser",
        "AuditProposal",
        "BindingStatus",
        "Budget",
        "CallTool",
        "Capability",
        "Claim",
        "ClaimSource",
        "ControlDecision",
        "ControlDecisionKind",
        "CoverageClaim",
        "Debt",
        "Effect",
        "EffectKind",
        "EvidenceStatus",
        "Finish",
        "FsDelete",
        "FsWrite",
        "GapProposal",
        "ModelView",
        "NetworkFact",
        "Obligation",
        "ObligationKind",
        "ObligationSource",
        "Opaque",
        "PlanProposal",
        "PolicyDecision",
        "PolicyDecisionKind",
        "PropositionKind",
        "ReadFact",
        "ReceiptStatus",
        "ReleaseStep",
        "Resolution",
        "ResolutionStatus",
        "Risk",
        "StepContract",
        "StepKind",
        "SubmitPlan",
        "TaskMode",
        "TaskPhase",
        "TaskResult",
        "TaskSpec",
        "TestRun",
        "Think",
        "ToolBinding",
        "ToolCall",
        "ToolIntent",
        "ToolReceipt",
        "ToolSpec",
        "UpdatePlan",
        "VcsCommit",
        "VcsPush",
        "Verdict",
        "VerdictStatus",
        "VerificationStage",
    }
)
_PHASES = frozenset(
    {
        "LEGAL_TRANSITIONS",
        "TERMINAL_PHASES",
        "assert_transition",
        "is_legal_transition",
    }
)
_PROTOCOLS = frozenset(
    {
        "ApprovalProvider",
        "EventSink",
        "Ledger",
        "Policy",
        "Runtime",
        "RuntimeServices",
        "Strategy",
        "ToolCatalog",
        "ToolGateway",
        "VerificationController",
    }
)
_AUDIT = frozenset({"AuditCoordinator", "CoverageBinder"})

_MODULE = {
    **{name: "xenon.kernel.contracts" for name in _CONTRACTS},
    **{name: "xenon.kernel.phases" for name in _PHASES},
    **{name: "xenon.kernel.protocols" for name in _PROTOCOLS},
    **{name: "xenon.kernel.audit" for name in _AUDIT},
}

__all__ = sorted(_MODULE)


def __getattr__(name: str) -> Any:
    module = _MODULE.get(name)
    if module is None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
