# Xenon

[![CI](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml/badge.svg)](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/xianyu-sheng/Xenon/branch/main/graph/badge.svg)](https://codecov.io/gh/xianyu-sheng/Xenon)
[![Release](https://img.shields.io/github/v/release/xianyu-sheng/Xenon)](https://github.com/xianyu-sheng/Xenon/releases)
[![GitHub stars](https://img.shields.io/github/stars/xianyu-sheng/Xenon?logo=github)](https://github.com/xianyu-sheng/Xenon/stargazers)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

<p align="center">
  <img src="docs/logo-animated.gif" alt="Xenon — a pixel runner sprints along the middle line and dissolves into the blocky XENON wordmark" width="440">
</p>

<h3 align="center">One loop. One ledger. One replayable truth.</h3>

<p align="center">The model proposes; it cannot turn an unverified effect into a delivery.<br/>Xenon records every tool call as a typed receipt, binds every claim to a fact, and makes unverifiable work explicit debt or abstention.</p>

<p align="center">
  <b>SWE-bench Lite (blind)</b>: <b>62.2%</b> frozen 30 · <b>52.2%</b> zero-overlap held-out · <b>4,000+</b> tests · <b>Python 3.10–3.12</b>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#kernel-architecture">Architecture</a> ·
  <a href="#evidence-and-failure-semantics">Evidence</a> ·
  <a href="#benchmark">Benchmark</a> ·
  <a href="docs/GUIDE.md">Docs</a> ·
  <a href="https://github.com/xianyu-sheng/Xenon/issues">Issues</a> ·
  <a href="README.md">简体中文</a>
</p>

> [!TIP]
> **The model proposes; the harness proves.** Xenon does not use an LLM judge or seven isomorphic engines. It keeps one `LoopRuntime`: the model emits capability actions, the catalog binds tools, the gateway returns receipts, the controller decides repair/debt, and the finalizer owns delivery commits.

## Quick start

```bash
pip install https://github.com/xianyu-sheng/Xenon/releases/download/v0.9.5/xenon_harness-0.9.5-py3-none-any.whl
xenon
```

Windows: add pip's Scripts directory to `PATH`, or use `python -m xenon`.

## Kernel architecture

```text
TaskSpec
  → LoopRuntime (single writer, explicit phase machine)
      view      = Projection(ledger[0..seq], catalog, spec)
      action    = Strategy.propose(view)        # model proposal only
      decision  = Policy.check(view, action)    # deterministic veto / approval
      receipt   = Gateway.execute(action)       # only side-effect entrance
      verdict   = Controller.reconcile(...)     # V0 / V1 / V2
      event     = Runtime.append(...)           # only durable write
  → Finalizer (stage verified paths, create delivery commit)
```

### Seven invariants

- **I1 single writer**: only `LoopRuntime` appends ledger events.
- **I2 single entrance**: tool effects go through `ToolGateway`; delivery commits belong to `Finalizer`.
- **I3 single source of truth**: the ledger is the only truth; everything else is a read-only projection with `input_seq`.
- **I4 semantic interface**: the kernel sees capabilities / obligations / receipts; tool names live only in the catalog.
- **I5 proposal/fact separation**: LLM output is `claim-*` / `hyp-*`; only a binder produces `fact-*`.
- **I6 single decision authority**: `VerificationController` decides repair / replan / abstain / abort.
- **I7 replayable**: the same ledger prefix produces byte-identical projections with zero network access.

## A turn

```text
input
  → TaskSpec (objective / obligations / budget / permission)
  → model proposal (think / call_tool / submit_plan / update_plan / finish / abort)
  → Catalog: capability → ToolSpec → ToolCall
  → Policy: allow / veto / require_approval
  → Gateway: seven-stage ToolExecutor → typed ToolReceipt
  → Controller: V0 receipt invariants / V1 step postconditions / V2 coverage
      bound       → continue
      unresolved  → debt / repair / replan
      ambiguous   → abstain, never guess
      abstained   → ask the user or keep debt
  → Finalizer: preflight audit → intent → commit → receipt → final decision
      non-deliver → roll back this run's commit
  → Projection: /evidence /goal /resume /plan read the same ledger
```

## Tools and MCP

- `ToolCatalog` exposes **seven capabilities**: `read / search / edit / verify / vcs / shell / remote`.
- MCP uses **per-tool ToolSpecs**; the model gets a deterministic budgeted subset, not every tool.
- Unknown tools stay `unresolved`; approval is the remote-call boundary and one-shot grants expire.
- `git push` remains vetoed until an explicit `TaskSpec.allow_push` gate.

## Evidence and failure semantics

| Tier | Check | Authority |
|---|---|---|
| **V0** | exit code, no-op write, target, real test target, commit sha, worktree delta | controller |
| **V1** | whether a step's `expects` were closed by facts | controller |
| **V2** | global obligation coverage, negative facts, semantic gaps | deterministic binder + user abstention |

- Zero matches are `unresolved`; multiple matches are `ambiguous`; neither silently picks a fact.
- Unverifiable semantic requirements are `abstained`, never promoted to pass.
- Debt blocks delivery until a fact exists or the user explicitly accepts the gap.
- The hash chain proves process integrity, not that the model's conclusion is correct.

## Benchmark

| Item | Value |
|---|---|
| Protocol | Fully blind: no official test patch during solving, no FAIL_TO_PASS names in prompt |
| Engine | Single `react` (not a multi-engine union / best-of-N) |
| Model | `deepseek-v4-flash`, official API |
| Judging | Official `swebench` harness |
| Repetition | Three independent runs, mean reported; every run published |

| Sample | Current framework | Previous v0.8.5 |
|---|---:|---:|
| Frozen 30 (seed 20260729) | **62.2%** | 63.3% |
| Zero-overlap held-out (seed 20261016) | **52.2%** | 46.7% |

Full protocol, per-run results and sha256 archives: [EVAL_RESULTS](docs/EVAL_RESULTS.md).

## Featured in

- [Awesome-MCP-ZH](https://github.com/yzfly/Awesome-MCP-ZH)
- [awesome-opensource-ai](https://github.com/alvinreal/awesome-opensource-ai)
- [awesome-mcp-lists](https://github.com/collabnix/awesome-mcp-lists)
- [awesome-ai-coding-tools](https://github.com/mahmoudsajjadi/awesome-ai-coding-tools)

## Common commands

```text
/evidence   ledger, V2 coverage, chain verification
/goal       objective, phase, plan, debt
/resume     restore the session and ledger pointer
/plan       soft plan mode
/mode       choose a reasoning mode
/mcp        MCP servers and tools
/rewind     rewind to a turn node
/fork       fork from a turn node
```

## Documentation

- [Operation guide](docs/GUIDE.md)
- [Architecture](docs/ARCHITECTURE.en.md) · [ADR 0001](docs/adr/0001-single-loop-kernel.md)
- [Harness](docs/HARNESS.md)
- [Extending](docs/EXTENDING.en.md)
- [Evaluation](docs/XENON_EVAL_PROTOCOL.md) · [Results](docs/EVAL_RESULTS.md)
- [Changelog](CHANGELOG.md)

## Honest boundaries

- The hash chain proves process integrity, not conclusion truth.
- Abstention is a product behavior, not a hidden failure.
- Approval is not a sandbox; run external MCP servers in a controlled environment.
- SWE-bench numbers are limited by sample and budget; read them with EVAL_RESULTS.

## License

MIT
