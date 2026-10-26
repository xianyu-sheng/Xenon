"""Kernel contracts: the semantic interface between the loop and its consumers.

This module intentionally contains **no behavior** and imports only the standard
library.  It must never import the legacy engine, tool handlers or the REPL.
The purity guard in ``scripts/check_kernel_purity.py`` enforces that rule.

Terminology:

* A **capability** is what the loop can ask for (read/search/edit/verify/vcs/
  shell/remote).  Concrete tool names belong to the catalog, not the kernel.
* An **effect** is a fact produced by one tool execution: a file hash, a test
  result, a commit, a workspace delta.  The ledger stores facts; the model
  produces claims.
* **V0/V1/V2** are execution verification tiers.  They are unrelated to Xenon's
  existing L0/L1/L2 trust layers (evidence/structure/semantics).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

SCHEMA_VERSION = "kernel/v1"


# ── capabilities and effects ───────────────────────────────────────────────


class Capability(str, Enum):
    """Closed capability set.  Adding one is a contract change, not a config."""

    READ = "read"
    SEARCH = "search"
    EDIT = "edit"
    VERIFY = "verify"
    VCS = "vcs"
    SHELL = "shell"
    REMOTE = "remote"


class EffectKind(str, Enum):
    FS_WRITE = "fs_write"
    FS_DELETE = "fs_delete"
    TEST_RUN = "test_run"
    VCS_COMMIT = "vcs_commit"
    VCS_PUSH = "vcs_push"
    READ = "read"
    NETWORK = "network"
    OPAQUE = "opaque"


class Risk(str, Enum):
    INFO = "info"
    WRITE = "write"
    SENSITIVE = "sensitive"


@dataclass(frozen=True, slots=True)
class FsWrite:
    """Files changed by one call.  ``changed=False`` means a real no-op."""

    paths: tuple[str, ...]
    hashes: Mapping[str, str] = field(default_factory=dict)
    changed: bool = True
    created_paths: tuple[str, ...] = ()

    @property
    def kind(self) -> EffectKind:
        return EffectKind.FS_WRITE


@dataclass(frozen=True, slots=True)
class FsDelete:
    paths: tuple[str, ...]
    changed: bool = True

    @property
    def kind(self) -> EffectKind:
        return EffectKind.FS_DELETE


@dataclass(frozen=True, slots=True)
class TestRun:
    """Structured test result.  ``tests_run == 0`` is not a passing test."""

    # Pytest otherwise tries to collect this dataclass as a test class.
    __test__ = False

    target: str
    framework: str = "pytest"
    tests_run: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    exit_code: int = 0
    command: str = ""

    @property
    def kind(self) -> EffectKind:
        return EffectKind.TEST_RUN


@dataclass(frozen=True, slots=True)
class VcsCommit:
    sha: str
    branch: str = ""
    files: tuple[str, ...] = ()

    @property
    def kind(self) -> EffectKind:
        return EffectKind.VCS_COMMIT


@dataclass(frozen=True, slots=True)
class VcsPush:
    remote: str
    branch: str
    sha: str = ""

    @property
    def kind(self) -> EffectKind:
        return EffectKind.VCS_PUSH


@dataclass(frozen=True, slots=True)
class ReadFact:
    path: str
    content_hash: str = ""

    @property
    def kind(self) -> EffectKind:
        return EffectKind.READ


@dataclass(frozen=True, slots=True)
class NetworkFact:
    url: str
    status: int = 0

    @property
    def kind(self) -> EffectKind:
        return EffectKind.NETWORK


@dataclass(frozen=True, slots=True)
class Opaque:
    """A shell effect observed from the workspace, not declared by the tool."""

    command: str
    exit_code: int = 0
    workspace_delta: tuple[str, ...] = ()

    @property
    def kind(self) -> EffectKind:
        return EffectKind.OPAQUE


Effect = (
    FsWrite
    | FsDelete
    | TestRun
    | VcsCommit
    | VcsPush
    | ReadFact
    | NetworkFact
    | Opaque
)


# ── task and phases ────────────────────────────────────────────────────────


class TaskMode(str, Enum):
    TASK = "task"
    CHAT = "chat"
    AUTO = "auto"


class TaskPhase(str, Enum):
    CREATED = "created"
    INTAKE = "intake"
    RUNNING = "running"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    RECONCILING = "reconciling"
    REPAIRING = "repairing"
    REPLANNING = "replanning"
    AUDITING = "auditing"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    FINALIZED = "finalized"
    BLOCKED = "blocked"
    ABORTED = "aborted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Budget:
    max_tokens: int | None = None
    max_wall_seconds: float | None = None
    max_steps: int | None = None


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    objective: str
    workspace: str
    constraints: tuple[str, ...] = ()
    mode: TaskMode = TaskMode.AUTO
    deadline: float | None = None  # absolute epoch seconds; see Budget for duration
    budget: Budget = field(default_factory=Budget)
    schema_version: str = SCHEMA_VERSION


# ── catalog and receipts ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    pack: str
    capabilities: frozenset[Capability]
    effects: frozenset[EffectKind]
    risk: Risk
    params_schema: Mapping[str, Any] = field(default_factory=dict)
    description: str = ""
    preferences: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolIntent:
    capability: Capability
    params: Mapping[str, Any] = field(default_factory=dict)
    targets: tuple[str, ...] = ()
    prefer: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    spec_name: str
    params: Mapping[str, Any] = field(default_factory=dict)


class BindingStatus(str, Enum):
    BOUND = "bound"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class ToolBinding:
    """Capability -> tool resolution.  Ambiguity is explicit, never silent."""

    status: BindingStatus
    call: ToolCall | None = None
    candidates: tuple[str, ...] = ()
    reason: str = ""


class ReceiptStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    DENIED = "denied"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class ToolReceipt:
    receipt_id: str
    call_id: str
    task_id: str
    tool: str
    status: ReceiptStatus
    exit_code: int | None = None
    step_id: str | None = None
    effects: tuple[Effect, ...] = ()
    declared_effects: tuple[EffectKind, ...] = ()
    observed_effects: tuple[EffectKind, ...] = ()
    raw_ref: str = ""
    error: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0


# ── obligations, claims, verdicts ──────────────────────────────────────────


class PropositionKind(str, Enum):
    FILE_CHANGED = "file_changed"
    FILE_CREATED = "file_created"
    TEST_PASSED = "test_passed"
    COMMIT_CREATED = "commit_created"
    PUSHED = "pushed"
    NO_STRAY_CHANGES = "no_stray_changes"
    REQUIREMENT_MET = "requirement_met"
    ANSWER_DELIVERED = "answer_delivered"
    TOOL_SUCCEEDED = "tool_succeeded"


ObligationKind = PropositionKind


class ObligationSource(str, Enum):
    USER = "user"
    PLAN = "plan"
    POLICY = "policy"


class ClaimSource(str, Enum):
    LLM = "llm"
    USER = "user"


@dataclass(frozen=True, slots=True)
class Obligation:
    id: str
    kind: PropositionKind
    target: str = ""
    expect: Mapping[str, Any] = field(default_factory=dict)
    source: ObligationSource = ObligationSource.USER
    required: bool = True
    since_seq: int = 0


@dataclass(frozen=True, slots=True)
class Claim:
    id: str
    kind: PropositionKind
    target: str = ""
    expect: Mapping[str, Any] = field(default_factory=dict)
    source: ClaimSource = ClaimSource.LLM
    since_seq: int = 0


class ResolutionStatus(str, Enum):
    BOUND = "bound"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    ABSTAINED = "abstained"


@dataclass(frozen=True, slots=True)
class Resolution:
    claim_id: str
    status: ResolutionStatus
    fact_ids: tuple[str, ...] = ()
    reason: str = ""
    candidates: tuple[str, ...] = ()


class VerificationStage(str, Enum):
    """Execution verification tiers, intentionally not named L0/L1/L2."""

    V0 = "v0"
    V1 = "v1"
    V2 = "v2"


class VerdictStatus(str, Enum):
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class Verdict:
    obligation_id: str
    stage: VerificationStage
    status: VerdictStatus
    fact_ids: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Debt:
    obligation: Obligation
    reason: str
    attempts: int = 0
    next_action: str = ""
    user_accepted: bool = False


@dataclass(frozen=True, slots=True)
class CoverageClaim:
    """A V2 proposal: which artifacts/tests are believed to cover an obligation."""

    obligation_id: str
    artifact_paths: tuple[str, ...] = ()
    test_targets: tuple[str, ...] = ()
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class GapProposal:
    """A V2 proposal for what is still missing.  Never a pass/fail verdict."""

    obligation_id: str
    reason: str
    next_action: str = ""


@dataclass(frozen=True, slots=True)
class AuditProposal:
    """LLM output for V2.  It is a hypothesis set, not a decision."""

    coverage: tuple[CoverageClaim, ...] = ()
    gaps: tuple[GapProposal, ...] = ()
    rationale: str = ""


# ── plan and actions ───────────────────────────────────────────────────────


class StepKind(str, Enum):
    INSPECT = "inspect"
    EDIT = "edit"
    VERIFY = "verify"
    COMMIT = "commit"
    CLEAN = "clean"
    EXECUTE = "execute"
    THINK = "think"


@dataclass(frozen=True, slots=True)
class StepContract:
    id: int
    kind: StepKind
    intent: ToolIntent | None = None
    targets: tuple[str, ...] = ()
    expects: tuple[Obligation, ...] = ()
    depends_on: tuple[int, ...] = ()
    prefer: str | None = None


@dataclass(frozen=True, slots=True)
class PlanProposal:
    steps: tuple[StepContract, ...]
    analysis: str = ""


class ActionKind(str, Enum):
    THINK = "think"
    CALL_TOOL = "call_tool"
    SUBMIT_PLAN = "submit_plan"
    UPDATE_PLAN = "update_plan"
    REQUEST_VERIFICATION = "request_verification"
    ASK_USER = "ask_user"
    FINISH = "finish"
    ABORT = "abort"


@dataclass(frozen=True, slots=True)
class Think:
    text: str

    @property
    def kind(self) -> ActionKind:
        return ActionKind.THINK


@dataclass(frozen=True, slots=True)
class CallTool:
    intent: ToolIntent

    @property
    def kind(self) -> ActionKind:
        return ActionKind.CALL_TOOL


@dataclass(frozen=True, slots=True)
class SubmitPlan:
    plan: PlanProposal

    @property
    def kind(self) -> ActionKind:
        return ActionKind.SUBMIT_PLAN


@dataclass(frozen=True, slots=True)
class UpdatePlan:
    plan: PlanProposal

    @property
    def kind(self) -> ActionKind:
        return ActionKind.UPDATE_PLAN


@dataclass(frozen=True, slots=True)
class RequestVerification:
    claim: Claim

    @property
    def kind(self) -> ActionKind:
        return ActionKind.REQUEST_VERIFICATION


@dataclass(frozen=True, slots=True)
class AskUser:
    question: str

    @property
    def kind(self) -> ActionKind:
        return ActionKind.ASK_USER


@dataclass(frozen=True, slots=True)
class Finish:
    answer: str
    claims: tuple[Claim, ...] = ()

    @property
    def kind(self) -> ActionKind:
        return ActionKind.FINISH


@dataclass(frozen=True, slots=True)
class Abort:
    reason: str

    @property
    def kind(self) -> ActionKind:
        return ActionKind.ABORT


Action = (
    Think
    | CallTool
    | SubmitPlan
    | UpdatePlan
    | RequestVerification
    | AskUser
    | Finish
    | Abort
)


# ── decisions and results ──────────────────────────────────────────────────


class PolicyDecisionKind(str, Enum):
    ALLOW = "allow"
    VETO = "veto"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    kind: PolicyDecisionKind
    reason: str = ""
    scope: tuple[str, ...] = ()
    ttl_seconds: float | None = None


class ControlDecisionKind(str, Enum):
    ADVANCE = "advance"
    REPAIR = "repair"
    REPLAN = "replan"
    ABSTAIN = "abstain"
    BLOCK = "block"
    ABORT = "abort"
    DELIVER = "deliver"


@dataclass(frozen=True, slots=True)
class ControlDecision:
    kind: ControlDecisionKind
    reason: str = ""
    obligation_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelView:
    task: TaskSpec
    system: str
    tools: tuple[Mapping[str, Any], ...]
    working_set: tuple[Mapping[str, Any], ...]
    debt: tuple[Debt, ...]
    budget: Budget
    input_seq: int
    hash: str
    phase: TaskPhase = TaskPhase.RUNNING
    schema_version: str = SCHEMA_VERSION


class EvidenceStatus(str, Enum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"
    ABSTAINED = "abstained"


@dataclass(frozen=True, slots=True)
class TaskResult:
    task_id: str
    accepted: bool
    phase: TaskPhase
    answer: str = ""
    claims: tuple[Claim, ...] = ()
    resolutions: tuple[Resolution, ...] = ()
    verdicts: tuple[Verdict, ...] = ()
    debt: tuple[Debt, ...] = ()
    finalize_sha: str | None = None
    evidence_status: EvidenceStatus = EvidenceStatus.UNVERIFIED
    metrics: Mapping[str, float] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
