# Xenon Architectural Design (v1.0.0)

> English | [中文版](ARCHITECTURE.md)
>
> *Note: This translation lags the source documentation; the [Chinese version](ARCHITECTURE.md)
> is the authoritative source of truth. Last synced around 2026-11-12 (§0 is current).*

> **Design Philosophy: The machine manages evidence; the human verifies truth.**
>
> The machine is solely responsible for "procedural authenticity" — what was done, success or failure metrics, and whether the evidence chain is intact. For "content correctness", it provides tiered, transparent disclosures, returning semantic judgment back to the user. This is the fundamental distinction between Xenon and agent runtimes that blindly trust model outputs.

## 0. The single-loop kernel (current control flow)

> This section is the authoritative view of the **control flow** (mirrored from the Chinese
> doc). Everything from §1 onward is the implementation/credibility view of the same system.

### 0.1 Why one loop

The old Xenon had seven "reasoning engines". `engine/registry.py` admitted itself that the
seven built-in paradigms **ran an identical flow** — only the class, a few parameters, the
mode line and the panel title differed. The same loop was copied seven times and tool
semantics lived in five places. Now there is exactly one control flow:

```text
TaskSpec
  → LoopRuntime (single writer, explicit phase machine)
      view      = Projection(ledger[0..seq], catalog, spec)
      action    = Strategy.propose(view)        # the model only proposes
      decision  = Policy.check(view, action)    # deterministic veto / approval
      receipt   = Gateway.execute(action)       # the only side-effect entrance
      verdict   = Controller.reconcile(...)     # V0 / V1 / V2
      event     = Runtime.append(...)           # the only durable write
  → Finalizer (stages only verified paths, creates the delivery commit)
```

### 0.2 Kernel at a glance

```text
                    ┌──────────── LoopRuntime (single writer) ─────────┐
 TaskSpec ─────────▶│ intake → running → tool_call → reconciling      │
                    │        → auditing → delivering → finalized      │
                    │ view      = Projection(ledger, catalog, spec)   │
                    │ action    = Strategy.propose(view)              │
                    │ decision  = Policy.check(action)                │
                    │ receipt   = Gateway.execute(action)             │
                    │ verdict   = Controller.reconcile(...)           │
                    │ event     = Ledger.append(...)                  │
                    └─────────────────────────────────────────────────┘
          ▲                       │                        │
     Projections              ToolCatalog               EventSink
  ModelView / TaskState     core + MCP pack          TUI / JSONL / SDK
  PlanView / EvidenceView
```

### 0.3 The seven invariants (I1–I7)

- **I1 single writer**: one writer per ledger at any moment. In-process, `Ledger.open_shared`
  shares one instance; before a cross-process append the tail is compared and the ledger is
  reloaded if it moved (`_refresh_tail_if_moved`) — never a silent fork.
- **I2 single entrance**: tool side effects go through `ToolGateway`; delivery commits belong
  to `Finalizer`.
- **I3 single source of truth**: the ledger is the only truth; everything else is a read-only
  projection carrying an `input_seq`.
- **I4 semantic interface**: the kernel only knows capability / obligation / receipt; tool
  names live in the catalog.
- **I5 proposal/fact separation**: model output is `claim-*` / `hyp-*`; only the binder
  produces `fact-*`.
- **I6 single decision right**: `VerificationController` decides repair / replan / abstain /
  abort.
- **I7 replayable**: the same ledger prefix yields byte-identical projections, with zero
  network during replay.

### 0.4 Verification tiers (V0 / V1 / V2)

| Tier | Check | Authority |
|---|---|---|
| **V0** | exit code, no-op write, target match, real test target, commit sha, worktree delta | controller |
| **V1** | whether a step's `expects` were closed by a fact | controller |
| **V2** | global obligation coverage, negative facts, semantic gap | deterministic binder + user abstention |

- A tool failure is never guessed into a success by keywords;
- zero matches is `unresolved`, multiple matches is `ambiguous` — neither silently picks one;
- semantic requirements that cannot be verified deterministically are `abstained`, not passed;
- debt blocks delivery until a fact exists or the user explicitly accepts it;
- the hash chain proves the process was not tampered with; it does not prove the conclusion.

### 0.5 Tools and MCP

- `ToolCatalog` expresses tool semantics with **8 capabilities**:
  `read / search / discover / edit / verify / vcs / shell / remote`;
- the `discover` capability (`find_tools`) queries the catalog by intent before a call,
  returns bounded candidates with full parameters and never executes — free in every
  permission mode;
- MCP uses a **per-tool ToolSpec**, not "dump every tool into the model";
- tool budgets are chosen deterministically from the objective (e.g. 46 GitHub tools → 16
  injected); unknown tools are `unresolved`;
- approval is the boundary for external MCP calls; each grant binds a target and carries a
  TTL, and is revoked right after the call;
- `git push` stays vetoed until an explicit `TaskSpec.allow_push`.

## 1. Three-Tier Trust Model

> ⚠️ **THIS ENGLISH TRANSLATION LAGS THE CODE (last updated 2026-11-12).** §0 mirrors the
> current kernel control flow; sections 1–7 are the implementation view and were written
> before the single-loop rewrite. The [Chinese version](ARCHITECTURE.md) remains the
> authority. Two important shifts postdate most of sections 2–7:
>
> 1. The **seven engine registry has been removed**.  There is now one kernel
>    loop, `LoopRuntime`; "modes" are just `LoopProfile` declarations in
>    `xenon/modes.py` (not an "engine registry").  References below to
>    `engine/builtin_engines.py` and "7 paradigm executors" are historical.
> 2. **One live path.**  The kernel (`LoopRuntime`, ledger in `ledger/`) is the
>    only execution and verification chain.  The legacy `TurnGate /
>    HollowDetector` path was unreachable and has been **deleted**; the old L0
>    session log (`session/events.py`) survives only because `/resume`,
>    `/evidence`, `/rewind` and `/goal` still read it.
>
> Treat sections 2–7 as describing the legacy path and defer to the Chinese doc
> wherever they conflict.

| Tier | Mechanism | Location | Machine Responsibility |
|---|---|---|---|
| **L0 Evidence Tier** | **Current**: the kernel ledger = append-only hash chain (`ledger/`); `verify_tree` verifies child branches with it. **Historical**: the session event log (`session/events.py`, still backs `/resume` and `/evidence`) | `ledger/store.py`, `ledger/tree.py`; legacy `session/events.py`, `repl/command_groups/evidence.py` | Prove process authenticity; verify chain integrity (unkeyed anchor, see README honest boundaries) |
| **L1 Structural Tier** (retired · 2026-11-11) | Historical hollow-response check. The kernel expresses "did any work happen" through receipts and debt | (was `turn/gate.py`, `engine/hollow_detector.py` — deleted) | No blocking structural check remains |
| **L2 Semantic Tier** (kernel equivalent) | Claims bound to receipts, completion criteria, coverage audit: never blocks delivery; partial/unverified runs attach the "needs human review" panel | `kernel/controller.py`, `repl/render.py` (`render_verification_reminders`) | Prominently highlight unverified claims for the user to verify |

Invariants:
- **Evidence is a label, never a gate**: the kernel decides delivery and `FinalizeGuard` is the only deterministic hard check; `tests/test_contract_single_source.py` locks the contract entry points repository-wide.
- **Tool success or failure is determined solely by the `success` field in the tool receipt** (single semantic channel); panels, events, and rendering never guess status via keywords.
- Within engines, only deterministic structural corrections occur (rewriting incomplete responses, retrying empty completion claims without tool usage); no LLM semantic judgment is performed inside engines.

## 2. System Overview

```text
                    ┌──────────────────────────────────────────┐
                    │        REPL / CLI (repl/)                │
                    │  Input rendering · commands · approvals  │
                    └───────────────┬──────────────────────────┘
                                    │ Turn orchestration (kernel_host / turn_flow)
              ┌─────────────────────┼───────────────────────┐
              ▼                     ▼                       ▼
      TurnContract          Kernel (kernel/, one loop)   Session (session/, legacy)
      Intent classification + LoopRuntime + phase config tree / events (still backs
      regex guards → policy   projections / compaction /  /resume · /evidence)
                              loop detection · tokens     / archive / projection
              │                     │                       │
              └──────────┬──────────┴───────────┬───────────┘
                         ▼                      ▼
                    ToolExecutor             FinalizeGuard + evidence labels
                    7-stage pipeline         verified / partial / unverified
                    policy · fence · retry              │
                         │                              ▼
                    ToolNode / families      Ledger append (ledger/) + replayable views
```

## 3. Turn Lifecycle (Single State Machine)

```text
User Input
  → TurnContract: LLM intent classifier primary + regex guards constraint (fail-closed fallback)
  → Loop execution (kernel/): phase progression, loop signals, circuit breaker,
    token budget + compaction, honest truncation marking + continuation
  → Kernel delivery (FinalizeGuard is the only deterministic gate):
      finalized → Deliver with evidence labelled verified / partial / unverified;
                  partial and unverified attach the "needs human review" panel (non-blocking)
      blocked / aborted / failed → the title says so; even verified evidence
                  never makes a stopped run read as finished
  → Ledger event append (ledger/, replayable read-only projections);
    the legacy session path additionally persists TurnTree nodes and turn archives
```

Node states have five values: `passed / degraded / fused / interrupted / pending-approval` (`session/tree.py`). Resumable states are `fused / interrupted / failed-retried`; a bare "continue" inherits cause and progress from the resumable node.

## 4. Module Boundaries

```text
xenon/
├── kernel/      Single-loop kernel (the only decision point): runtime · controller ·
│                phases · contracts · policies (permission modes) · strategies ·
│                view (read-only projection) · finalize · audit · session_grants ·
│                session_history · loop_advice
├── ledger/      Evidence chain: store (single writer + crash repair) · events (hash
│                chain) · tree (branch verification) · retention (opt-in) ·
│                projections · migrate (explicit legacy import) · conversation
├── catalog/     Capability catalog (single source of tool names): catalog · specs ·
│                gateway · finalizer · worktree
├── harness/     Headless orchestration: runner · llm (prompts/tool calls) ·
│                child (subprocess + branch seal) · intake · plan · cli · sink · summarizer
├── repl/        Interaction layer: REPL main loop · runtime_adapter (kernel turns) ·
│                turn_flow (legacy turns) · direct_chat · render · approval_ux ·
│                session_ops · turn_contract · task_state · command_groups (auto-discovery)
├── frontend/    Protocol host: host (NDJSON); the built TS UI ships inside the wheel
│                (assets/xenon-ui.js; Node.js 20+ at runtime)
├── session/     Legacy L0 chain (still backs /resume · /evidence · /rewind · /goal):
│                tree · events · archive · projection · query
├── engine/      Execution layer (historical directory name): callbacks · context ·
│                circuit_breaker · evidence_gate (GateVerdict only) · tool_runtime ·
│                tool_tracker · tool_profiles · tool_gate · scheduler · task_recipe
├── nodes/       Tool layer: tool_executor (7 stages: classify → validate → approve →
│                execute → retry → audit) · tool_node (path fencing / command blocklist) ·
│                tool_registry · tool_families (12 families) · approval_policy ·
│                param_validation · path_targets
├── hooks/       Local hooks: 4 events · project-level trust fingerprints · PreToolUse fail-closed
├── sandbox/     Sandbox seam: read-only mode · command allowlist (chain-aware) · backends (process/docker/wsl)
├── memory/      Memory: user / project-local / project-shared / session (4 scopes)
├── mcp/         MCP: client (stdio transport · registry · tool injection) · server
│                (stdio JSON-RPC exposing task / evidence / audit tools)
└── utils/       llm_client (transport / continuation) · llm_transport · llm_usage ·
                 redact · errors (one error envelope) · credentials_io · project_root ·
                 github_auth · atomic_write · code_index
```

## 5. Security Boundaries (Actual Implementations)

| Boundary | Mechanism | Key Location |
|---|---|---|
| Path Fencing | Resolves symlinks and junctions to real paths; rejects UNC / `\\?\` / 8.3 short paths; falls back safely on loops and path-length overflows | `nodes/tool_node.py` `_validate_path` |
| Dangerous Commands | Quote-aware + segment-anchored blocklist; rejects shell substitutions and variable expansions | `tool_node._validate_command` |
| Boundary Approval | Writes inside workspace require no prompt; execution-grade tools (`command`, `spawn_agent`, execution-type `mcp_call`) always prompt; `y` grants `(tool, target_path)`, `a` records session TTL rule; non-interactive fails closed | `repl/approval_ux.py`, `nodes/approval_policy.py` |
| Subagents | Read-only by default (`min(parent, READ_ONLY)`); approval channels inherit parent context | `engine/react_engine.py` |
| register_tool | Module allowlist + dangerous module rejection + explicit rejection of credential/config modules | `tool_node._validate_register_module` |
| Hooks | User-level always trusted; project-level requires fingerprint trust (`/trust-hooks`); PreToolUse timeout or crash fails closed | `hooks/runner.py` |
| Sandbox | Read-only mode + deterministic rejection of non-allowlisted commands (chain-aware); honestly declared as "software detection boundary, not full OS isolation" | `sandbox/__init__.py` |

## 6. Extension Contracts

```python
MODE_CATALOG[mode].loop                     # xenon/modes.py (sole source: 7 mode names, 6 profiles)
register_tool(name, handler, params)      # Tool registration (with security allowlist)
register_command("/name", desc, usage)    # command_groups auto-discovery, no central file
# Local hooks: <user|project>/.xenon/hooks.yaml (PreToolUse / PostToolUse /
#              Stop / SubagentStop, JSON stdin, exit code 2 blocks PreToolUse)
# Custom provider: register_custom_provider → credentials.yaml _custom_providers
# MCP: stdio server registered tools are injected into engine (shares evidence chain with ToolExecutor)
```

`xenon/modes.py` is the **sole** source of mode metadata for `/mode` and kernel routing. Every
mode is just a `LoopProfile` (which phases run, how many calls per step, how many reflection
rounds); there is no second executor. `react` and `direct` declare an identical profile, so
`react` is an alias of `direct` (the name is kept so `--mode react` still works).

## 7. Large File Modularization Status

| Phase | Target | Status |
|---|---|---|
| A | ToolNode → ToolRegistry + tool family modules (12 families) | ✅ Completed |
| B | REPL input extraction (`repl_input`) | ✅ Completed |
| C | Slash commands modularized by topic (`command_groups` auto-discovery) | ✅ Completed |
| R | REPL split (`repl` 2383 lines → 8 modules + Mixin composition) | ✅ Completed |
| D | Split ReAct/Base engine protocol parsing / tool loop / subagents | ⏳ Pending (`react_engine` 1872 lines / `base` 1857 lines) |
| E | LLM client split by transport (split `llm_transport`/`llm_usage`, main file 1532 lines) | ⏳ Partial |

## 8. Quality Gates (CI)

- Python 3.10 / 3.11 / 3.12 matrix + e2e + 3-platform (Ubuntu / Windows / macOS) install smoke tests.
- Code coverage threshold **70%** (actual test ~74%, `omit = []`).
- Ruff rules contract explicitly pinned (`select = E4/E7/E9/F/I/B`, where `I` and `B` are the isort and bugbear phases of #127), tool versions can be freely upgraded.
- Single source of truth for version: `xenon.__version__` (dynamically read by `pyproject.toml`).
- Single source invariants tests (`test_semantics_single_source.py`, `test_contract_single_source.py`) regularly run in CI.
