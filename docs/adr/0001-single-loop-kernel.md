# ADR 0001 — Single-loop verifiable harness kernel

- Status: accepted for implementation (#192)
- Date: 2026-10-25
- Supersedes: multi-engine runtime (`engine/react_engine.py`, `plan_execute_engine.py`, `combined_engines.py`, `reflection_engine.py`, `engine/registry.py`)
- Related: #193–#204, #205

## 1. Context

Xenon currently ships seven reasoning "engines". `engine/registry.py` documents the
truth itself: the seven built-ins have "completely isomorphic run flows"; only the
engine class, a few kwargs, the mode line and the panel title differ.

The core-profile experiment made the cost visible. Changing the model-facing tool
surface required edits in five independent places:

1. planner prompt (static tool list),
2. plan validation (write/verify step detection),
3. `TaskState` (artifact/test/commit inference from tool names),
4. `ClaimGate` / delivery (obligation inference from tool names),
5. `ToolExecutor` profile guard (`_tool_profile` read from `AgentContext`).

A real task created files, ran two passing tests, made a commit and left a clean
worktree — and delivery still reported "missing test evidence". The problem was not
missing evidence. It was **five components interpreting the same event differently**.

At the same time Xenon already has the organs a single-loop harness needs:
`tool_executor` (seven-stage pipeline), MCP, sandbox, `loop_detector`,
`circuit_breaker`, `budget`, `hollow_detector`, structured context compaction and a
hash-chained evidence ledger. The rotten part is the skeleton, not the organs.

## 2. Research: what good coding harnesses actually do

This is a design survey, not a claim that closed-source internals are observable.
For closed products we use publicly visible behavior and Xenon's own comparison
document (`docs/COMPARISON.md`, `docs/EXECUTION_LAYER_GAPS.md`).

| System | Control flow | Planning | Verification | Context | State / evidence |
| --- | --- | --- | --- | --- | --- |
| Claude Code | one agent loop; plan mode and subagents are modes, not separate runtimes | plan/todo as state | runs commands/tests; human approval | automatic compaction | session state; not event-sourced in public docs |
| Codex | one loop; explicit approvals / sandbox | plan/todo | tests + sandbox | compaction | turn state |
| Aider | one edit loop | repo map, no plan runtime | lint/test + auto-commit | repo map + chat files | git as durable state |
| SWE-agent | one ReAct-style loop | none | tests via ACI | trajectory | trajectory |
| OpenHands | event stream + agent controller + runtime | planner is an agent | runtime observations | event history | **event-sourced** |
| SmartBench | analysis pipeline | ProjectReader proposes hypotheses | deterministic resolver + validator + CFG | EvidencePack | **facts vs hypotheses, explicit abstention** |
| Xenon today | seven runtimes | separate plan engine | five retry authorities | per-engine context | two ledgers, snapshot fallback |

What the successful systems share:

- **One control loop.** Planning, acting, reviewing and routing are policies or
  modes inside that loop, not parallel runtimes with their own prompts and state.
- **One durable state channel.** OpenHands' event stream is the closest public
  reference; Xenon's own `docs/EVENT_SOURCE.md` already states the same rules.
- **Verification is a first-class step, not a post-hoc report.** Tests, diffs, exit
  codes and workspace state are checked where the effect happens.
- **Explicit human boundaries.** Approval, sandbox and "I cannot verify this" are
  product surfaces, not silent fallbacks.
- **Context is a view.** Compaction/summarization is derived from durable state; it
  is not a second source of truth.

What does **not** survive the comparison:

- "Multi-engine" as a differentiator. Seven isomorphic loops are seven copies of the
  same control flow, and every copy interprets tools differently.
- An LLM judge as the final authority. It is not reproducible, not auditable, and it
  hides uncertainty instead of exposing it.

## 3. Decision

Xenon becomes a **single-loop, event-sourced, evidence-first harness**.

```text
TaskSpec
  → LoopRuntime (single writer, explicit phase machine)
      view      = Projection(ledger[0..seq], catalog, spec)
      action    = Strategy.propose(view)        # LLM proposes only
      decision  = Policy.check(view, action)    # deterministic veto
      receipt   = Gateway.execute(action)       # only side-effect entrance
      verdict   = Controller.reconcile(...)     # V0 / V1
      event     = Runtime.append(...)           # only durable write
  → V2 audit (deterministic coverage + LLM proposals)
  → Delivery / Finalize
```

### 3.1 Roles

| Role | Owns | Must not |
| --- | --- | --- |
| `LoopRuntime` | phase transitions, ledger writes, action scheduling | interpret tool names, write prompts, decide completion |
| `Strategy` | proposing the next `Action` from a `ModelView` | execute tools, write state, decide delivery |
| `Policy` | allow / veto / require approval | execute, write state, bypass controller |
| `ToolCatalog` | specs, packs, capability binding | execute, decide retries |
| `ToolGateway` | the only side-effect entrance; produces `ToolReceipt` | decide retries or delivery |
| `VerificationController` | V0/V1 reconciliation, debt, bounded repair decisions | call tools directly, overwrite ledger facts |
| `Projection` | pure view over `ledger[0..seq]` | write, hold mutable state, re-infer facts |
| LLM | plans, next actions, repairs, coverage hypotheses | write facts, mark pass, override V0/V1, choose retry counts |

### 3.2 Phase machine

```text
CREATED → INTAKE → RUNNING
RUNNING ⇄ {THINKING, TOOL_CALL, RECONCILING, REPAIRING, REPLANNING}
RUNNING → AUDITING → DELIVERING → DELIVERED → FINALIZED
any non-terminal → BLOCKED | ABORTED | FAILED
```

Only `LoopRuntime` may transition. Every transition appends a `state_transition`
event. A projection that disagrees with the machine is a projection bug; a machine
that disagrees with the ledger is a runtime bug. There is no third place to fix.

### 3.3 Plan is an action, not a runtime

- `SubmitPlan` / `UpdatePlan` are `Action`s.
- `PlanFirst` is a `Strategy`; `ActFirst` is another; `NoTool` is the direct-chat
  strategy.
- DAG scheduling is a `SchedulerPolicy`, not an engine.
- Triage is a hint (`initial_strategy`), not a router engine.
- Repair is a bounded strategy fed by a structured rejection; it cannot see or
  change the validator.

This keeps the useful behavior (plan panel, step contracts, DAG, repair) while
removing the duplicated runtimes.

### 3.4 Verification tiers: V0 / V1 / V2

> Terminology: Xenon already has **L0/L1/L2 trust layers** (evidence / structure /
> semantics). The execution tiers below are deliberately named **V0/V1/V2** so the
> two models never get conflated. See #205.

| Tier | When | What | Authority |
| --- | --- | --- | --- |
| **V0** | every receipt | deterministic invariants: exit code, no-op write, target match, real test target, real commit sha, workspace delta | controller |
| **V1** | step close | did this step's declared `expects` close? | controller |
| **V2** | task audit | coverage of user obligations, negative facts, global consistency | deterministic binders + user for abstention |

Rules:

- V0/V1 are deterministic-first and run **inside** the loop, at the effect boundary.
- Unclosed obligations become `debt`; debt is carried to V2, not re-derived later.
- V2 only audits debt and global coverage; it does not re-run every local check.
- V2 may ask the LLM for `CoverageProposal` / `GapProposal`, but the LLM never emits
  pass/fail and never closes debt.
- If no deterministic check exists, the result is `ABSTAINED`; the harness asks the
  user. The user's acceptance is recorded as `user_accepted_debt`.

### 3.5 Evidence model: adapt SmartBench, do not copy it

SmartBench's trust boundary — deterministic analyzers own facts; the LLM may only
propose hypotheses; zero matches are `unresolved`, multiple matches are `ambiguous`,
both abstain — maps directly onto execution:

```text
ToolReceipt effect   → fact-*        (deterministic, content-addressed)
Model plan / answer  → claim-*       (proposal)
Audit coverage       → hyp-*         (proposal)
ClaimResolver        → bound | unresolved | ambiguous | abstained
```

What is **Xenon-specific**, not copied:

- The "fact" is an **effect**, not a static-analysis finding: file hash, test
  target/counts, commit sha, push, workspace delta.
- V0 runs on every tool call, not only on submitted candidates.
- Negative facts are first-class (`no push`, `no stray changes`, `no new deps`).
- Delivery is a product contract: `accepted`, `debt`, `finalize_sha`, `unverified`.
- `abstain` is surfaced in the terminal UI and `/evidence`, not buried in a report.

### 3.6 Direct chat

Direct chat enters the same loop as `TaskMode.CHAT` with `NoToolStrategy`:

- catalog is empty (or remote-only when the user explicitly asks);
- no required delivery obligations;
- the answer is a `claim-*` (`ANSWER_DELIVERED`), never a `fact-*`;
- the task is ledgered and replayable;
- it is marked `unverified_answer`, not "verified delivery";
- no tool history is injected into the working set (#165).

This gives every interaction an evidence trail without pretending that a chat
answer was verified.

## 4. Shell, approvals and auto-edit

`shell` stays. A terminal harness without a shell is not a terminal harness.

- `shell` declares no precise effects; the gateway observes pre/post workspace
  state and records `Opaque(command, exit_code, workspace_delta)`.
- Command classification (test / build / vcs / other) is deterministic and is used
  to synthesize `TestRun` / `VcsCommit` facts when applicable.
- We stop banning shell syntax (`mkdir`, redirection). We observe effects instead.
  This removes the `_CORE_SHELL_WRITE` false rejection and makes `bash git commit`
  bindable.
- Approval policy is an interface: `(capability, target, effect, mode) → allow |
  ask | deny`.
- **Initial default is conservative**: `auto_edit=off`; writes require approval.
  The interface is designed so auto-edit can be enabled later without changing the
  kernel.
- Destructive operations (delete, reset, force push, global install) are a separate
  policy and default to deny.

## 5. Migration

Strangler, not big bang:

1. New packages land beside the old engine (`kernel/`, `catalog/`, `ledger/`).
2. `XENON_RUNTIME=kernel` selects the new runtime; legacy remains default until
   parity.
3. Contract tests, replay, benchmark and real TTY tasks must pass on the kernel.
4. Flip the default; keep legacy for one release.
5. #202 deletes legacy engines, prompts, gates and vocabularies.

The old engine is a **reference oracle** for parity, not a dependency of the new
kernel.

## 6. Consequences

- `register_engine()` disappears. Extension contracts become:
  `register_tool_spec()`, `register_strategy()`, `register_policy()`,
  `register_projection()`.
- `docs/COMPARISON.md` must stop selling "7 种范式" as the differentiator. The
  differentiator becomes **verifiable execution**: every claim traces to a fact,
  every side effect has a receipt, every task is replayable.
- `README` positioning: **Xenon is a verifiable terminal harness. The model
  proposes; the harness proves.**
- New metrics: `false_completion_rate`, `unverified_claim_rate`,
  `side_effects_without_receipt`, `calls_to_completion`, `abstention_rate`,
  `replay_determinism`.
- The existing L0/L1/L2 trust model remains; V0/V1/V2 are execution verification
  tiers, and both are documented separately.

## 7. Open questions

1. **Direct chat UX**: how loudly should `unverified_answer` be shown? (No tool
   facts exist; we must not imply verification.)
2. **Auto-edit policy**: exact default matrix per capability and target; interface
   is fixed, values are configurable.
3. **Child runtime**: process-per-subagent vs process-pool; both share the parent
   receipt link contract.
4. **MCP dynamic packs**: how to refresh specs without leaking stale tool names into
   `ModelView`.
5. **TurnGate fate**: HollowDetector stays as an L1 structure sensor, but its retry
   authority moves to the controller. Which checks become V0/V1 sensors vs pure UI
   warnings must be decided in #199.

## 8. Rejected alternatives

- **Keep multi-engine, add a facade.** The five interpretation seams remain; a
  facade hides them instead of removing them.
- **Big-bang rewrite.** Loses the working execution organs and the reference
  oracle; no parity evidence during migration.
- **LLM judge for completion.** Not reproducible, not auditable, and contradicts
  Xenon's evidence-first positioning.
- **Another gate on top.** The problem is five authorities, not too few checks.

## 9. Invariants (normative)

- I1 single writer: only `LoopRuntime` appends to the ledger.
- I2 single entrance: every side effect goes through `ToolGateway`.
- I3 single source: ledger is truth; projections are pure and versioned.
- I4 semantic interface: kernel knows capabilities/obligations/receipts, not names.
- I5 proposal/fact separation: LLM output is `claim-*`/`hyp-*`; only deterministic
  binders create `fact-*`.
- I6 single decision authority: `VerificationController` decides repair/replan/
  abstain; all other gates are sensors.
- I7 replayable: same ledger → identical projection hashes, zero network.
