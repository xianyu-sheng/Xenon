# Xenon — Quick Start Guide

> English | [中文操作手册](OPERATION_GUIDE.md)

Xenon is a terminal coding agent with one control flow: a single `LoopRuntime` writes an
append-only ledger, every tool call becomes a typed receipt, and every claim the model makes
has to bind to one of those receipts. "Done" is a label the harness can check, not a sentence
the model wrote.

## Requirements

- **Python 3.10–3.12**
- **Node.js 20+** — the interactive UI is a TypeScript (Ink) front-end, run by Node.
  A wheel ships it prebuilt; a source checkout has to build it once.

```bash
# from a release wheel
pip install https://github.com/xianyu-sheng/Xenon/releases/download/v1.0.0/xenon_harness-1.0.0-py3-none-any.whl
xenon --version

# or from source
git clone --depth 1 https://github.com/xianyu-sheng/Xenon.git
cd Xenon && python -m venv .venv
.venv/Scripts/python -m pip install -e .        # Windows
source .venv/bin/activate && pip install -e .   # Linux / macOS

cd frontend && npm ci && npm run build:release && cd ..
```

On Windows, add pip's Scripts directory to `PATH`, or run `python -m xenon`.

## First run

```bash
xenon
```

With no credentials configured the UI says so and tells you to type `/setup`. The wizard asks
for a provider, an API key (masked), a model and a mode. Keys are stored encrypted at rest
(Windows DPAPI; `keyring` when available) and never printed back.

An environment variable works too — for example `DEEPSEEK_API_KEY=sk-...` before starting.

## Basic usage

```text
❯ /                                  # open the command palette (prefix or abbreviation)
❯ /prm                               # → /permissions; Enter runs the highlighted row
❯ @src/                              # file-path completion; ↑/↓ to choose, Tab to accept
❯ Shift+Tab                          # cycle modes (same as /mode next)
❯ Add a --dry-run flag to tools/publish.py and cover it with a test
```

Useful commands:

```text
/evidence     ledger, coverage, claims ↔ receipts, debt
/goal         objective, phase, plan, open obligations
/resume       restore a session and its ledger pointer
/plan         soft plan mode
/mode         choose a loop profile
/permissions  approval policy + session grants (revocable)
/model        switch the active model
/memory       cross-session memory (user / project / session)
/sessions     list and resume sessions
/mcp          MCP servers and tools
/tools        tools and their capabilities
```

## What a "mode" is now

There is exactly one loop. A mode is a set of phase parameters for it (`xenon/modes.py`),
not a different engine:

| Mode | Shape |
|---|---|
| `direct` | plan → execute with step scope; the default profile |
| `react` | alias of `direct` (kept so `--mode react` still works) |
| `plan-execute` | plan first, then follow the plan step by step |
| `reflection` | execute, then one reflection pass |
| `plan-react` | plan + per-step bounded tool calls (6 per step) |
| `plan-reflection` | plan + plan review + per-step reflection |
| `react-reflection` | execute, then a deeper reflection pass (3 rounds) |

## Evidence, not trust

```text
/evidence   # the same ledger the harness acted on
/goal       # objective, phase, plan, debt
```

A turn ends with a label — `verified` / `partial` / `unverified` — computed from receipts,
not from the model's prose. Claims that cannot be bound become debt; semantic requirements
that cannot be checked deterministically are reported as unverified rather than passed.
See [ARCHITECTURE.en.md](ARCHITECTURE.en.md) and the README's evidence section.

## Tools and MCP

Tools are described by capability (`read / search / discover / edit / verify / vcs / shell /
remote`), and the catalog decides which ones a task sees; `/tools` lists them.

```text
/mcp add <name> ...          # connect an external MCP server (client side)
xenon mcp-serve --workspace . --permission read-only
```

`mcp-serve` runs Xenon **as** an MCP server for other agents: `xenon_run_task`,
`xenon_evidence`, `xenon_audit`. Read-only and fail-closed by default. Details in
[MCP_SERVER](MCP_SERVER.md).

## Interfaces

| Entry | When |
|---|---|
| `xenon` | TS front-end (default, the product UI) |
| `xenon --ui python` | built-in raw-terminal editor (development / no Node) |
| `xenon exec "<task>"` | headless run, JSON-able output |

The fallback is explicit on purpose: there is no silent downgrade to a different UI.

## Where to read more

- [TUI.md](TUI.md) — layout, keys, approval panels
- [OPERATION_GUIDE.md](OPERATION_GUIDE.md) — full manual (Chinese)
- [ARCHITECTURE.md](ARCHITECTURE.md) — kernel, invariants, security boundaries
- [MCP_SERVER.md](MCP_SERVER.md) — serve Xenon to Claude Code and friends
- [EXTERNAL_AUDIT.md](EXTERNAL_AUDIT.md) — audit another agent's transcript
