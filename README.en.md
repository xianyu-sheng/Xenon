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

<h3 align="center">A terminal AI coding agent that shows you exactly how much was actually verified</h3>

<p align="center">Other agents say "done" and you take their word for it.<br/>Xenon splits the result in two: what the machine confirmed, and what you should check yourself.</p>

<p align="center">
  <a href="docs/GUIDE.md">Docs</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="CHANGELOG.md">Changelog</a> ·
  <a href="https://github.com/xianyu-sheng/Xenon/issues">Issues</a> ·
  <a href="https://github.com/xianyu-sheng/Xenon/releases">Releases</a> ·
  <a href="README.md">简体中文</a>
</p>

> [!TIP]
> **The machine owns the process; semantic judgement stays with you.** Every tool call goes into the evidence chain (L0); the repository has exactly one blocking check (L1); semantic doubts never block delivery — they ship as a manual-verification checklist next to the answer (L2).

<p align="center">
  <img src="docs/hero-terminal.en.svg" alt="Xenon terminal: what the machine confirmed in one delivery, and the two items it could not verify" width="860">
</p>

## Quick start

```bash
# Install from GitHub Releases (no PyPI required)
pip install https://github.com/xianyu-sheng/Xenon/releases/download/v0.9.4/xenon_harness-0.9.4-py3-none-any.whl

xenon   # first run walks you through setup (API key, model, engine)
```

> **Windows:** if you get `'xenon' is not recognized`, add the Scripts path pip printed to `PATH`, or use `python -m xenon` (available in release artifacts since v0.9.4). Mixing conda with a standalone Python? Use a dedicated venv: `python -m venv .venv`.

More install options (source / uv / pipx) are in [Install](#install).

## What it actually does

<table>
<tr><td><b>An evidence chain, not just logs</b></td><td>Every tool call is written to an append-only hash chain (content hash + predecessor hash); <code>/evidence</code> recomputes and self-checks it at any time, catching corruption, single-point tampering and reordering.</td></tr>
<tr><td><b>Exactly one blocking check</b></td><td>"Hollow answers" (no work done, non-answers) are deterministic — caught, fed back for a retry, and fused if the same cause repeats. Semantic doubts never block delivery; they ship as a manual-verification checklist next to the answer.</td></tr>
<tr><td><b>Every turn is a node in a tree</b></td><td><code>/rewind</code> goes back, <code>/fork</code> branches; after an interruption or fuse, typing "continue" resumes from that node without re-explaining context.</td></tr>
<tr><td><b>Boundary actions ask first</b></td><td>In-workspace writes don't interrupt you; out-of-boundary writes and commands open a unified panel with a diff preview, and approvals can be scoped by target and TTL. Subagents are read-only by default.</td></tr>
<tr><td><b>Untrusted output is tagged</b></td><td>File, web, GitHub, MCP and command output is tagged with its source and trust level; common prompt-injection patterns produce a visible warning, and the approval panel notes when an action may be driven by untrusted content. It only warns — nothing is blocked or rewritten.</td></tr>
<tr><td><b>Routing learns from your choices</b></td><td>When you override the auto-router with <code>/mode</code>, the preferred paradigm gets a small confidence boost for similar tasks; preferences stay local — delete <code>~/.xenon/routing_preferences.json</code> to reset them.</td></tr>
<tr><td><b>Limits are labeled honestly</b></td><td>Truncated files and exhausted context are marked as such instead of being papered over.</td></tr>
</table>

## How a turn runs

```text
your input
  → intent contract (LLM classifier first, conservative regex fallback)
  → engine execution (budget checkpoints / loop detection / circuit breaker)
  → turn gate (the single verdict point)
      pass    → deliver
      remind  → deliver + manual-verification checklist (non-blocking)
      fail    → retry in the same turn; budget exhausted → degraded delivery
      fuse    → fuse on repeated same-cause structural failure
  → turn tree persisted + hash chain appended + turn archived
```

## What is verified, and what is not

| Layer | What it checks | How it surfaces |
|---|---|---|
| **L0 Evidence** | Whether the process is real and replayable | Hash-chained event log; `/evidence` recomputes it |
| **L1 Structural** | Whether the answer skipped the work | The only blocking check: retry / same-cause fuse |
| **L2 Semantic** | Whether claims, criteria and file references hold up | Never blocks; produces a "manual verification checklist", or a degraded delivery after retries run out |

Semantic judgement lives in exactly one place (`xenon/turn/gate.py`); engines do no semantic evaluation. Tool success/failure comes only from the tool's own receipt — never from keyword guessing.

`/evidence` output (structured turn summary + a one-line chain self-check):

<img src="docs/evidence-chain.png" alt="/evidence output example" width="720">

## Common commands

```
/evidence   evidence chain (current / all / one session)   /plan   plan mode
/goal       set the goal for this session                  /review review edits with .bak revert
/rewind     go back to an earlier turn                     /fork   branch from a turn
/sessions   list sessions                                  /compact compact long conversations
/permissions show current approvals                        /tools  list tools
```

On start, `xenon` shows a session picker first (pick a history entry by number, or a fresh session); `xenon resume` restores the most recent one and replays the conversation.

## Install

```bash
# Option 1 — wheel from GitHub Releases (recommended, no PyPI required)
pip install https://github.com/xianyu-sheng/Xenon/releases/download/v0.9.4/xenon_harness-0.9.4-py3-none-any.whl
xenon --version   # verify the install

# Option 2 — from source (editable; shallow clone saves bandwidth)
git clone --depth 1 https://github.com/xianyu-sheng/Xenon.git
cd Xenon && python -m venv .venv
.venv/Scripts/python -m pip install -e .        # Windows
source .venv/bin/activate && pip install -e .   # Linux / macOS
```

`uv tool install <wheel URL>` and `pipx install <wheel URL>` work too and handle the entry point for you.

## Benchmark

SWE-bench Lite — same model (`deepseek-v4-flash`), same official harness, react engine, 120-step budget, blind protocol (no test patch, no test-name hints), three runs per cell averaged:

| Sample | Current framework | Previous generation (v0.8.5) |
|---|---|---|
| Frozen 30 instances (seed 20260729) | **62.2%** | 63.3% |
| Zero-overlap held-out sample (seed 20261016) | **52.2%** | 46.7% |

> The original sample carries in-sample tuning traces (9 of its 30 instances are named in the commit history), so the held-out sample is the evidence to trust; the relative gap flips sign between the two samples — the old generation's edge on the original sample came at least partly from tuning. Full protocol, per-run results, judging logs and sha256-checked archives: [EVAL_RESULTS](docs/EVAL_RESULTS.md).

## Documentation

| Topic | 中文 | English |
|---|---|---|
| Operation manual / quick start | [OPERATION_GUIDE](docs/OPERATION_GUIDE.md) | [GUIDE](docs/GUIDE.md) |
| Architecture | [ARCHITECTURE](docs/ARCHITECTURE.md) | [ARCHITECTURE.en](docs/ARCHITECTURE.en.md) |
| Extending (MCP / subagents / hooks / commands) | [EXTENDING](docs/EXTENDING.md) | [EXTENDING.en](docs/EXTENDING.en.md) |
| Comparison vs Aider / Claude Code / OpenCode / Crush | [COMPARISON](docs/COMPARISON.md) | [COMPARISON.en](docs/COMPARISON.en.md) |
| Tool-output threat model | [THREAT_MODEL](docs/THREAT_MODEL.md) | — |
| Development guide (test recipes / platform notes / brand pipeline) | [DEVELOPMENT](docs/DEVELOPMENT.md) | — |
| Observability (AgentOps) design, P0 | [OBSERVABILITY](docs/OBSERVABILITY.md) | — |
| TUI design and usage | [TUI](docs/TUI.md) | — |
| Eval protocol / eval results | [EVAL_PROTOCOL](docs/XENON_EVAL_PROTOCOL.md) · [EVAL_RESULTS](docs/EVAL_RESULTS.md) | — |
| Changelog | [CHANGELOG](CHANGELOG.md) | — |

## Environment variables

<details>
<summary>13 variables (click to expand)</summary>

| Variable | Purpose | Default |
|---|---|---|
| `XENON_APPROVAL_TTL` | TTL for session-level `a` approval rules (seconds) | 1800 |
| `XENON_VERIFY_RETRIES` | Same-turn retries after a structural failure | 2 |
| `XENON_RETRY_BUDGET_MIN/MAX` | Step-budget clamp for retry turns | 10 / 40 |
| `XENON_GATE_FUSE_THRESHOLD` | Same-cause structural failure fuse threshold | 2 |
| `XENON_MAX_ITERATIONS` | Steps per run (all engines, uniformly 120) | 120 |
| `XENON_MAX_TOKENS` | Output cap per model call | 8192 |
| `XENON_MAX_CONTINUATIONS` | Auto-continuations after truncation | 3 |
| `NO_COLOR` | Standard flag; disables color in output and prompt | unset |
| `XENON_READ_ONLY` | Read-only mode (all write/exec tools disabled) | off |
| `XENON_COMMAND_ALLOWLIST` | Command first-word allowlist (comma-separated) | empty = unrestricted |
| `XENON_SESSION_EVENTS(_DIR)` | Event log toggle / directory | on |
| `XENON_HOOKS` | Local hooks toggle | on |
| `GITHUB_TOKEN` / `GH_TOKEN` | GitHub tool auth (raises API limits) | empty |

</details>

## Platform support

- **Windows**: full support on Windows Terminal (ConPTY) + PowerShell (paste, CJK IME, arrow keys); `python -m xenon` ships in release artifacts since v0.9.4.
- **Linux / macOS**: any standard terminal; `XENON_TERMINAL_ANIMATION=0` disables the startup animation.

## Honest boundaries

- **The machine does not verify semantics.** When L2 hits it only warns — judge content correctness yourself with the checklist.
- **The hash chain proves process, not conclusions.** It shows the event stream was not tampered with; it does not prove the model was right.
- **The chain is an unkeyed integrity check.** It detects accidental corruption, single-point tampering and reordering; it cannot stop someone with write access from recomputing the whole chain. Put the event directory on controlled storage for stronger guarantees. (Optional HMAC anchor: see #106.)
- **The sandbox is detection + a software boundary, not real isolation.** Run untrusted code inside WSL/Docker.
- **Injection detection on tool output is a heuristic advisory, not a security boundary.** Xenon tags sources and warns about common injection patterns, but it never blocks or rewrites content. Scope and limitations: [tool-output threat model](docs/THREAT_MODEL.md).
- `/review`'s revert only covers edits with `.bak` backups; git is still the version history.
- The intent classifier uses the fastest model available (~1s); if unavailable it falls back to conservative regexes.

## Contributing

- Contribution flow and the PR "CI evidence" requirement: [CONTRIBUTING](CONTRIBUTING.md)
- Local setup, test recipes and platform differences: [DEVELOPMENT](docs/DEVELOPMENT.md)
- Starter tasks: [good first issues](https://github.com/xianyu-sheng/Xenon/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
- Contributor entry point (task board and how to claim): [#144](https://github.com/xianyu-sheng/Xenon/issues/144)
- Feature ideas / feedback / roadmap: [Roadmap & Feedback](https://github.com/xianyu-sheng/Xenon/discussions/143)
- Quality gates: full offline suite on Linux + Windows/macOS platform smoke + Codecov coverage; 3,000+ offline test cases
- If Xenon is useful to you, a ⭐ helps more people find it

## Featured in

<!-- Ordered by list prominence; insert new entries in place once merged. -->

- [Awesome-MCP-ZH](https://github.com/yzfly/Awesome-MCP-ZH) — curated Chinese MCP resource list (MCP Clients → verifiable terminal coding agent)
- [awesome-opensource-ai](https://github.com/alvinreal/awesome-opensource-ai) — open-source AI project list (Agentic AI & Multi-Agent Systems → Autonomous Coding Agents)
- [awesome-mcp-lists](https://github.com/collabnix/awesome-mcp-lists) — Collabnix community list of MCP servers, clients and toolkits (MCP Clients → Command Line Tools)
- [awesome-ai-coding-tools](https://github.com/mahmoudsajjadi/awesome-ai-coding-tools) — 3-tier taxonomy of AI coding tools (CLI coding agents; special thanks to @xianyu-sheng)

## License

MIT
