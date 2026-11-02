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
    WORKSPACE_PROBE = "workspace_probe"
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
    # Bounded content explicitly returned by a read tool.  The ledger keeps it
    # so the model can act on what it read; raw tool output is still never
    # copied into the model view.
    content: str = ""

    @property
    def kind(self) -> EffectKind:
        return EffectKind.READ


@dataclass(frozen=True, slots=True)
class WorkspaceProbe:
    """A deterministic look at the worktree, taken by the host (not the model).

    ``dirty_paths`` is every path the VCS reports as changed; ``verified_paths``
    is what the ledger can account for.  "No stray changes" therefore becomes a
    question with an answer instead of a semantic guess.
    """

    dirty_paths: tuple[str, ...] = ()
    verified_paths: tuple[str, ...] = ()
    head: str = ""


@dataclass(frozen=True, slots=True)
class NetworkFact:
    url: str
    status: int = 0
    # Bounded textual payload returned by an external tool (for MCP/read-only
    # remote calls).  Raw tool output is still not copied blindly; the gateway
    # only stores the explicit content field.
    content: str = ""

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
    | WorkspaceProbe
    | Opaque
)


# ── task and phases ────────────────────────────────────────────────────────


class TaskMode(str, Enum):
    TASK = "task"
    CHAT = "chat"
    AUTO = "auto"
    # Stop after the plan is accepted; execution waits for an external
    # approval.  This is a result mode, not a strategy shortcut.
    PLAN = "plan"


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
    PLANNED = "planned"
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
    # Repair/replan budgets are policy caps, not token budgets.  They are
    # counted by the runtime so a strategy cannot retry forever.
    max_repair_attempts: int = 2
    max_replans: int = 3
    # V2 audit rounds across repeated Finish attempts.  The first audit is
    # always allowed; a second unresolved round must block/abstain.
    max_audit_rounds: int = 1
    # Total context window of the model doing the work, in tokens.  0 means
    # "unknown", in which case the view falls back to a heuristic.  Knowing
    # the real window is what makes compaction trigger at the right point:
    # an 8k model and a 200k model should not share one magic multiplier.
    context_window: int = 0


# Caps mirror the context manager: a project snapshot or retrieved memory must
# never crowd out the working set or the action protocol.
_MAX_CONTEXT_BLOCK_CHARS = 20_000
_MAX_CONTEXT_TOTAL_CHARS = 40_000


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    objective: str
    workspace: str
    constraints: tuple[str, ...] = ()
    mode: TaskMode = TaskMode.AUTO
    deadline: float | None = None  # absolute epoch seconds; see Budget for duration
    budget: Budget = field(default_factory=Budget)
    # #201: push is an explicit user request, never a default finalize action.
    allow_push: bool = False
    # #287: deliver despite gaps the harness could not verify (evidence stays
    # PARTIAL and the gap is recorded as user-accepted).  Never overrides a
    # violated verdict.
    accept_debt: bool = False
    schema_version: str = SCHEMA_VERSION
    # The user's own words, kept separate from the strategy-facing prompt.
    # Legacy callers may omit it; projections then fall back to ``objective``.
    user_objective: str = ""
    # Explicit V2 obligations supplied by the caller/intake layer.
    obligations: tuple[Obligation, ...] = ()
    # Session-level reference material the front-end wants the model to see:
    # project rules, retrieved memories, and similar.  It is **reference**,
    # never instruction and never evidence, and it is part of the view hash so
    # identical inputs produce an identical prompt.
    context_blocks: tuple[tuple[str, str], ...] = ()
    # Which task delegated this one (empty for a top-level task).  Recorded in
    # TASK_STARTED so the call tree is derivable from ledgers alone — a child
    # used to be traceable only through the naming convention in its task_id.
    parent_task_id: str = ""

    def __post_init__(self) -> None:
        """Reject a malformed task at the boundary, not three layers down.

        The legacy engines learned this the hard way: ``run(None)`` used to
        crash inside execution-policy text handling instead of saying "your
        objective is not a string".  The kernel keeps that lesson.
        """

        if not isinstance(self.parent_task_id, str):
            raise ValueError("parent_task_id must be a string")
        total = 0
        for label, text in self.context_blocks:
            if not str(label).strip():
                raise ValueError("context block label must not be empty")
            if not isinstance(text, str):
                raise ValueError("context block text must be a string")
            total += len(text)
            if len(text) > _MAX_CONTEXT_BLOCK_CHARS:
                raise ValueError(
                    f"context block {label!r} exceeds "
                    f"{_MAX_CONTEXT_BLOCK_CHARS} chars"
                )
        if total > _MAX_CONTEXT_TOTAL_CHARS:
            raise ValueError(
                f"session context exceeds {_MAX_CONTEXT_TOTAL_CHARS} chars"
            )
        for name, value in (
            ("task_id", self.task_id),
            ("objective", self.objective),
            ("workspace", self.workspace),
        ):
            if not isinstance(value, str):
                raise ValueError(f"{name} 必须为非空字符串")
            if name != "workspace" and not value.strip():
                raise ValueError(f"{name} 必须为非空字符串")


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
    # Set by the catalog at bind time.  Gateway-level policies use this to
    # verify that the bound tool actually advertises the requested capability.
    capability: Capability | None = None


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
    # True when the binding came from a *proposal* that pointed at real facts
    # rather than from a deterministic rule.  The facts exist, but "these facts
    # prove this requirement" is still a judgement — so such a verdict may
    # support delivery but must never upgrade evidence to VERIFIED.
    from_proposal: bool = False


@dataclass(frozen=True, slots=True)
class Debt:
    obligation: Obligation
    reason: str
    attempts: int = 0
    next_action: str = ""
    user_accepted: bool = False
    # Accepting a gap means "I have no evidence either way".  A gap that came
    # from *positive* evidence of failure (a violated verdict) is never
    # acceptable, or acceptance would become a way to ship broken work.
    acceptable: bool = True


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


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """One row of the deterministic V2 coverage matrix."""

    obligation_id: str
    kind: PropositionKind
    target: str
    status: ResolutionStatus
    fact_ids: tuple[str, ...] = ()
    reason: str = ""
    next_action: str = ""
    source: str = "binder"


@dataclass(frozen=True, slots=True)
class AuditReport:
    """V2 output: coverage/gaps plus optional verdicts.

    The report never closes debt by itself; the runtime/controller decides how
    unresolved and abstained rows become debt.
    """

    coverage: tuple[CoverageRow, ...] = ()
    gaps: tuple[GapProposal, ...] = ()
    verdicts: tuple[Verdict, ...] = ()
    proposal: AuditProposal | None = None


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
    RELEASE_STEP = "release_step"


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
class ReleaseStep:
    """Stop exploring the active step; close it with the evidence so far.

    This is a strategy signal, never a model action: ``parse_action`` cannot
    produce it.  The runtime still closes the step deterministically, so an
    unmet expectation becomes debt rather than a delivery.
    """

    reason: str = ""

    @property
    def kind(self) -> ActionKind:
        return ActionKind.RELEASE_STEP


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
    active_step: StepContract | None = None
    steps: tuple[StepContract, ...] = ()
    # Deterministic step-level verification for the active step.  Reflection
    # sits on top of this: it reacts to verdicts, it never produces them.
    step_verdicts: tuple[Verdict, ...] = ()
    # Seqs folded into the ``elided`` digest entry to stay inside the
    # context budget.  Runtime bookkeeping: it lets the ledger record that
    # compaction happened without changing what the model is allowed to see.
    compacted: tuple[int, ...] = ()
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
