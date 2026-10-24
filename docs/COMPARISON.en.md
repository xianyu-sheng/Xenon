# Xenon vs Aider / Claude Code / OpenCode / Crush

> English | [中文版](COMPARISON.md)
>
> *Note: This translation may lag behind the source documentation; the [Chinese version](COMPARISON.md) is the authoritative source of truth. (Synchronized with commit `730d198`).*

> This is an **objective, evidence-based** capability comparison matrix. ✅ = Supported, ⚠️ = Partial / Limited, ❌ = Not Supported, — = Not Applicable.
> Evaluation date: 2026-07-08; Xenon capabilities updated to v0.9.2 status (2026-10-12).
> Note: Closed-source competitors (Claude Code / Crush) have internal implementations that cannot be directly observed; ❌ for them should be interpreted as "not found in publicly verifiable information" rather than an absolute assertion of absence.

---

## One-Line Summaries

| Project | Summary |
| --- | --- |
| **Xenon** | **MCP + Multi-model + Multi-paradigm all-in-one**, open-source terminal coding agent |
| [Aider](https://aider.chat) | Multi-model + git commit loop, specialized in repo-level editing |
| [Claude Code](https://claude.com/claude-code) | Official Claude + MCP terminal tool, closed source |
| [OpenCode](https://opencode.ai) | Multi-model + 50+ providers, rich TUI, written in Go |
| [Crush](https://github.com/charmbracelet/crush) | Multi-model + MCP + LSP enhancement, written in Go |

---

## Capability Matrix

| Dimension | Xenon | Aider | Claude Code | OpenCode | Crush |
| --- | :---: | :---: | :---: | :---: | :---: |
| **MCP Protocol** | ⚠️ stdio + SSE (simplified; no endpoint discovery / session correlation, see #103) | ❌ | ✅ First-class | ✅ | ✅ stdio / http / sse |
| **Multi-Model Routing** | ✅ 12 providers | ✅ ~10 | ❌ Claude only | ✅ 50+ | ✅ 18+ |
| **Multi-Paradigm Engines** | ✅ **7 paradigms** | ❌ | ❌ | ❌ | ⚠️ Mid-session LLM switch |
| **Local-First** | ✅ Ollama + local credentials | ⚠️ CLI local, cloud model | ❌ | ✅ | ✅ Ollama / llama.cpp |
| **Tool Circuit Breaker** | ✅ 3-failure trip | ❌ | ❌ | ❌ | ❌ |
| **Context Compaction** | ✅ 6 steps @ 80% | ❌ | ✅ | ❌ | ❌ |
| **Hollow Response Detection** | ✅ 15 regex patterns | ❌ | ❌ | ❌ | ❌ |
| **Three-Stage Budget** | ✅ Explore / Execute / Converge | ❌ | ❌ | ❌ | ❌ |
| **Open Source License** | ✅ MIT | ✅ Apache 2.0 | ❌ Closed source | ✅ MIT | ✅ FSL-1.1-MIT |
| **Implementation Language** | Python 3.10+ | Python | TypeScript | Go | Go |

---

## Dimension Breakdown

### 1. MCP Protocol

| Project | Capabilities |
| --- | --- |
| **Xenon** | Complete stdio transport; SSE is a simplified implementation (no `endpoint` discovery or streaming response ID correlation, see [#103](https://github.com/xianyu-sheng/Xenon/issues/103)); subprocesses use `select` + wall-clock timeout (fixed in v0.2.0 B11), preventing infinite `readline` hangs; process exit uses `terminate()` + fallback `kill()`, **zero zombie processes**. |
| Aider | Does not support MCP. Uses custom plugin mechanism to extend tools. |
| Claude Code | First-class, native MCP support; Anthropic proprietary protocol. |
| OpenCode | Supports MCP. |
| Crush | Supports MCP (stdio, http, and sse types); does not support automatic restart of MCP servers. |

**Xenon Advantages**: Complete stdio transport with B11 wall-clock timeout safeguards (fixed in v0.2.0 `bf6e83a`), ensuring a hanging child process will not stall the entire agent; current SSE is a simplified implementation (see [#103](https://github.com/xianyu-sheng/Xenon/issues/103)), and should not be viewed as parity with Crush's full transport support.  
**Xenon Trade-offs**: Unlike Claude Code, MCP servers do not automatically restart if a child process terminates; re-adding requires `/mcp add`.

### 2. Multi-Model Routing

| Project | Capabilities |
| --- | --- |
| **Xenon** | 12 providers configured in one place: `openai` / `anthropic` / `deepseek` / `ark` / `google` / `zhipu` / `qwen` / `moonshot` / `baichuan` / `minimax` / `xiaomi` / `ollama` (including local models); role-based `provider_priority`; per-provider persistent `httpx.Client` connection pools (v0.2.0 R3 fix). |
| Aider | ~10 providers; compatible with any OpenAI-like protocol. |
| Claude Code | Claude series models only (Opus / Sonnet / Haiku). |
| OpenCode | 50+ providers via Catwalk auto-discovery. |
| Crush | 18+ built-in providers; supports OpenAI / Anthropic compatible endpoints. |

**Xenon Advantages**: On par with Aider / Crush for multi-provider connectivity, but features an **automatic circuit breaker degradation mechanism** — cooling down automatically after 3 consecutive failures.  
**Xenon Trade-offs**: Provider count is smaller than OpenCode (50+) and Crush (18+), but 12 covers the primary mainstream and local offerings.

### 3. Multi-Paradigm Engines (**Core Differentiator**)

| Project | Capabilities |
| --- | --- |
| **Xenon** | **7 reasoning paradigms**: `direct` / `react` / `plan-execute` / `reflection` + 3 combined engines (`plan-react` / `plan-reflection` / `react-reflection`); switched via `/mode` in the same REPL. |
| Aider | Single paradigm: chat + tool call loop. |
| Claude Code | Single paradigm: single agent loop. |
| OpenCode | Single paradigm. |
| Crush | "Flexible: switch LLMs mid-session preserving context" — this is switching models within the same paradigm, not switching paradigms. |

**Xenon Advantages**: **This is the strongest differentiator in the entire table**. 7 paradigms enable:
- Simple Q&A → `direct` (saves tokens)
- Tool invocation → `react` classic loop
- Multi-step tasks → `plan-execute` automatic decomposition (PlanDAG topological parallelism)
- Quality-sensitive tasks → `reflection` with an independent reviewer model
- Creative writing → handled by generic `direct` / `reflection` modes without maintaining specialized out-of-scope engines
- Composite workflows → 3 combined engines (isolated context + reviewer model for multi-paradigm collaboration)

None of the other 4 projects support true multi-paradigm switching — Aider, Claude Code, and OpenCode use a single agent loop, while Crush switches the LLM mid-session without changing the paradigm.

### 4. Local-First

| Project | Capabilities |
| --- | --- |
| **Xenon** | Ollama local models; credentials stored in `~/.xenon/credentials.yaml`, no external network call; complete local state in `~/.xenon/` (compact / memory / sessions). |
| Aider | CLI runs locally; model calls route to cloud. |
| Claude Code | Closed source + cloud only. |
| OpenCode | Open source, supports Ollama. |
| Crush | Open source, full support for Ollama / llama.cpp / LM Studio / LiteLLM / omlx. |

**Xenon Advantages**: On par with Crush (Ollama + local credentials + local state).  
**Xenon Trade-offs**: Local runtime support is less granular than Crush (Crush supports 5 local runtime backends, whereas Xenon supports Ollama).

### 5. Tool Circuit Breaker

| Project | Capabilities |
| --- | --- |
| **Xenon** | Independent `CircuitBreaker` per tool, `failure_threshold=3` (default 3 consecutive failures) → transitions to `OPEN` state, with `cooldown=30s` (doubles on half-open failure, max 600s); independent registry per ToolExecutor instance. |
| Other 4 | ❌ None. |

**Xenon Advantages**: **Unique production-grade feature**. Other tools only retry or fail; they lack circuit breaking and exponential backoff. When a tool repeatedly fails, Xenon enters an OPEN state and rejects further calls immediately, protecting LLM tokens from spinning fruitlessly.

### 6. Context Compaction

| Project | Capabilities |
| --- | --- |
| **Xenon** | `Compactor` 6-step structured compactor triggered when token window reaches 80%; automatically compacts every 5 turns inside the engine to suppress O(n²) growth (v0.2.0 F4 fix); persisted to `~/.xenon/compact/`. |
| Claude Code | ✅ Supported (internal implementation details unannounced). |
| Other 3 | ❌. |

### 7. Hollow Response Detection

| Project | Capabilities |
| --- | --- |
| **Xenon** | `HollowDetector` with 3 signal classes: ① `len < 5` fail-fast; ② disproportionate `tool >= 5 && len < 100`; ③ 15 anti-pattern regular expressions + composite logic (regex match AND (insufficient length OR poor structure)). |
| Other 4 | ❌. |

**Xenon Advantages**: **Unique in the comparison table**. Hollow responses are the only blocking validation in the L1 structural tier (`turn/gate.py`): violations trigger a same-turn retry or same-cause circuit break, while semantic issues go through the non-blocking L2 review checklist.

### 8. Three-Stage Budget

| Project | Capabilities |
| --- | --- |
| **Xenon** | `BudgetManager`'s three-stage soft budget only activates when a bounded step limit is explicitly configured. With the default `XENON_MAX_ITERATIONS=0` (unlimited), ratio is always 0, phase stays EXPLORE and CONVERGE never fires. Token/time redesign or removal is tracked in #184. |
| Other 4 | ❌. |

**Xenon Advantages**: **Unique in the comparison table**. Rather than just asking an LLM for an answer, Xenon rhythmically guides the model toward the final resolution.

---

## How to Choose

| Your Scenario | Recommended |
| --- | --- |
| Fast git-commit-style code edits | **Aider** (mature, deep git integration) |
| Exclusively using Claude models with MCP | **Claude Code** (official, first-class MCP) |
| Need 50+ providers with an attractive TUI | **OpenCode** |
| Fast, lightweight Go binary with multiple local runtimes | **Crush** |
| **Need multi-paradigm (7 modes) + 12 providers + MCP + engineering triple-pack** | **Xenon** |
| Researching agent paradigms (PlanDAG / Reflection / combined engines) | **Xenon** (ideal experimental platform) |
| Chinese / local ecosystem focus | **Xenon** (DeepSeek / Qwen / Ollama suite) |

---

## Known Roadmap Items

- MCP server automatic restart (planned in #19 v0.3.0 roadmap).
- Expanding provider coverage beyond 12.
- Broadening local runtime options beyond Ollama.

Before filing an issue, check [CHANGELOG.md](../CHANGELOG.md) to confirm your version.
