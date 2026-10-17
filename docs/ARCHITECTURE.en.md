# Xenon Architectural Design (v0.9.2)

> English | [中文版](ARCHITECTURE.md)
>
> *Note: This translation may lag behind the source documentation; the [Chinese version](ARCHITECTURE.md) is the authoritative source of truth. (Synchronized with commit `730d198`).*

> **Design Philosophy: The machine manages evidence; the human verifies truth.**
>
> The machine is solely responsible for "procedural authenticity" — what was done, success or failure metrics, and whether the evidence chain is intact. For "content correctness", it provides tiered, transparent disclosures, returning semantic judgment back to the user. This is the fundamental distinction between Xenon and agent runtimes that blindly trust model outputs.

## 1. Three-Tier Trust Model

| Tier | Mechanism | Location | Machine Responsibility |
|---|---|---|---|
| **L0 Evidence Tier** | Session event log = append-only hash chain (each event contains content hash + predecessor hash); `/evidence` can recompute and self-verify at any time | `session/events.py`, `repl/command_groups/evidence.py` | Prove process authenticity; verify chain integrity (unkeyed anchor, see README honest boundaries) |
| **L1 Structural Tier** | Hollow responses (deterministic, zero semantic blindspots) = sole blocking check; only on hit will it retry (half-budget) or trigger same-cause circuit break | `turn/gate.py`, `engine/hollow_detector.py` | Block responses that failed to perform actual work |
| **L2 Semantic Tier** | Claim auditing / completion criteria / file claims / post-write verification: does not block delivery, attaches an "Unverified Checklist for User Review" | `turn/gate.py`, `repl/render.py` | Prominently highlight unverified claims for the user to verify |

Invariants:
- **Semantic judgment has only a single source of truth** (`turn/gate.py`), locked by repository-wide assertions in `tests/test_semantics_single_source.py` / `test_contract_single_source.py`.
- **Tool success or failure is determined solely by the `success` field in the tool receipt** (single semantic channel); panels, events, and rendering never guess status via keywords.
- Within engines, only deterministic structural corrections occur (rewriting incomplete responses, retrying empty completion claims without tool usage); no LLM semantic judgment is performed inside engines.

## 2. System Overview

```text
                    ┌──────────────────────────────────────────┐
                    │        REPL / CLI (repl/)                │
                    │  Input Rendering · Commands · Approvals  │
                    └───────────────┬──────────────────────────┘
                                    │ Turn Orchestration (engine_host / turn_flow)
              ┌─────────────────────┼───────────────────────┐
              ▼                     ▼                       ▼
      TurnContract              Engine (engine/)         Session (session/)
      Intent classification +   7 paradigm executors     Tree / events (hash chain)
      regex guards → strategy   Budget / loop detection  / archive / projection
              │                     │                       │
              └──────────┬──────────┴───────────┬───────────┘
                         ▼                      ▼
                    ToolExecutor             TurnGate (turn/gate.py)
                    7-stage pipeline         pass / remind / fail / fuse
                    policy · fence · retry              │
                         │                              ▼
                    ToolNode / Families      Tree persistence + event append + archive
```

## 3. Turn Lifecycle (Single State Machine)

```text
User Input
  → TurnContract: LLM intent classifier primary + regex guards constraint (fail-closed fallback)
  → Engine Execution: Budget checkpoints (progress → approve continuation; no progress → fuse),
    LoopDetector, circuit breaker, honest truncation marking + continuation
  → TurnGate Judgment (Sole decision point):
      pass   → Deliver
      remind → Deliver + human verification checklist (L2, non-blocking)
      fail   → Same-turn feedback retry (budget = initial steps / 2, clamped to [10, 40])
               Depleted → Degraded delivery (degraded + verification checklist)
      fuse   → Circuit break on repeated structural failure, delivered as incomplete
  → TurnTree node persistence + event hash chain append + structured turn archiving
```

Node states have five values: `passed / degraded / fused / interrupted / pending-approval` (`session/tree.py`). Resumable states are `fused / interrupted / failed-retried`; a bare "continue" inherits cause and progress from the resumable node.

## 4. Module Boundaries

```text
xenon/
├── repl/        Interaction layer: REPL main loop · engine_host (turn orchestration) ·
│                turn_flow · direct_chat · render · approval_ux · session_ops ·
│                turn_contract · task_state · command_groups (auto-discovery)
├── turn/        Turn validation: gate.py (L1 blocking / L2 reminder / same-cause fuse)
├── session/     Structure layer: tree (session tree) · events (hash chain event log) ·
│                archive (turn archives) · projection (goal projection) · query
├── engine/      Execution layer: registry + builtin_engines (7 paradigms; direct is not an engine);
│                react / plan_execute / reflection / combined; base (protocol);
│                evidence_runtime (L0 ledger) · evidence_gate · task_verifier ·
│                hollow_detector · loop_detector · circuit_breaker · budget
├── nodes/       Tool layer: tool_executor (7 stages: classify → validate → approve →
│                execute → retry → audit) · tool_node (path fencing / command blocklist) ·
│                tool_registry · tool_families ×9 · approval_policy · path_targets
├── hooks/       Local hooks: 4 events · project-level trust fingerprints · PreToolUse fail-closed
├── sandbox/     Sandbox seam: read-only mode · command allowlist (chain-aware) · backend detection
├── memory/      Memory: user / project-local / project-shared / session (4 scopes)
├── mcp/         MCP: stdio transport (select + wall-clock timeout) · registry · tool injection
└── utils/       llm_client (transport / continuation) · llm_transport · llm_usage ·
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
register_engine(name, factory, ...)       # engine/registry.py (sole source for 7 paradigms)
register_tool(name, handler, params)      # Tool registration (with security allowlist)
register_command("/name", desc, usage)    # command_groups auto-discovery, no central file
# Local hooks: <user|project>/.xenon/hooks.yaml (PreToolUse / PostToolUse /
#              Stop / SubagentStop, JSON stdin, exit code 2 blocks PreToolUse)
# Custom provider: register_custom_provider → credentials.yaml _custom_providers
# MCP: stdio server registered tools are injected into engine (shares evidence chain with ToolExecutor)
```

The engine registry is the sole source for paradigm metadata (`engine/builtin_engines.py`): `direct` has a factory of `None` — it is a pure REPL dialogue branch, not an engine; the remaining 6 (`react`, `plan-execute`, `reflection`, plus 3 combinations) are real engine implementations.

## 7. Large File Modularization Status

| Phase | Target | Status |
|---|---|---|
| A | ToolNode → ToolRegistry + tool family modules (9 families) | ✅ Completed |
| B | REPL input extraction (`repl_input`) | ✅ Completed |
| C | Slash commands modularized by topic (`command_groups` auto-discovery) | ✅ Completed |
| R | REPL split (`repl` 2376 lines → 8 modules + Mixin composition) | ✅ Completed |
| D | Split ReAct/Base engine protocol parsing / tool loop / subagents | ⏳ Pending (`react_engine` 1872 lines / `base` 1857 lines) |
| E | LLM client split by transport (split `llm_transport`/`llm_usage`, main file 1423 lines) | ⏳ Partial |

## 8. Quality Gates (CI)

- Python 3.10 / 3.11 / 3.12 matrix + e2e + 3-platform (Ubuntu / Windows / macOS) install smoke tests.
- Code coverage threshold **70%** (actual test ~74%, `omit = []`).
- Ruff rules contract explicitly pinned (`select = E4/E7/E9/F/I/B`, where `I` and `B` are the isort and bugbear phases of #127), tool versions can be freely upgraded.
- Single source of truth for version: `xenon.__version__` (dynamically read by `pyproject.toml`).
- Single source invariants tests (`test_semantics_single_source.py`, `test_contract_single_source.py`) regularly run in CI.
