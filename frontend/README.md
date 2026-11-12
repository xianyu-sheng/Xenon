# xenon-ui

TypeScript terminal front-end for Xenon. **The kernel stays in Python.**

This process only speaks the NDJSON protocol in
[`docs/design/frontend-protocol.md`](../docs/design/frontend-protocol.md): it
sends user intent, renders what the kernel reports, and answers the one
blocking question the kernel asks. It never computes evidence and never holds
authorization — both would let a UI bug (or a UI rewrite) change what the
product claims.

## Run

```bash
npm install
npm run build
node dist/cli.js --model deepseek/deepseek-v4-flash
```

The kernel must be importable — install it once from the repo root:

```bash
pip install -e . --break-system-packages
```

## What it does today

Steps 1–2 of the protocol doc: this is the whole UI. The Python side renders
nothing.

- **All 62 slash commands** run through the kernel and print their output here.
  Command handlers return rich markup; the host strips it at the boundary, so
  nothing arrives as literal `[dim]…[/dim]`.
- inline command completion (`/mo` → `/model`, `/mode`), Tab to cycle,
  Enter to accept — and Enter *runs* once the name is complete
- turn timeline (`think` / `tool` / `observe` / `step` / …)
- live progress and provider retry/timeout notices
- approval panel with the four kernel answers
  (`本次允许` / `本轮允许` / `全部允许` / `拒绝`, plus cancel)
- evidence status and ledger path, exactly as the kernel reported them
- scrollback held in Ink's `<Static>` so earlier output is never redrawn away
- **clickable paths**: file paths and the ledger path become OSC-8 hyperlinks,
  so a written file or a ledger can be opened without copy-paste. Ported from
  the Python `repl/clickable_paths.py`, which was built, tested, and never
  wired to any renderer — this is that wiring, in the layer that renders.

Not yet: model/mode pickers as list widgets, `/resume` history replay, diff
panes. The underlying commands work; only dedicated widgets are missing.

## Layout

| File | Role |
|---|---|
| `src/protocol.ts` | wire types; mirrors the doc, never invents a field |
| `src/host.ts` | spawns and drives the Python host |
| `src/state.ts` | pure reducer over frames — the part worth testing |
| `src/links.ts` | OSC-8 hyperlinks for paths in output |
| `src/ui.tsx` | rendering only |
| `src/App.tsx` | input handling and wiring |

## Tests

```bash
npm test        # reducer + parser, built first by `npm run build`
npm run typecheck
```

The reducer tests are the ones that matter: a UI that drops or invents a frame
is worse than one that renders plainly.

## Packaging

`npm run build` is the tsc output used by tests and local development.
`npm run build:release` additionally produces `dist/xenon-ui.js`: one
self-contained ESM bundle with Ink and React inlined, so the wheel does not
depend on `node_modules`. `python scripts/build_frontend_assets.py` stages that
bundle into `xenon/frontend/assets/`, which is what a wheel ships and what
`xenon` executes (CI verifies its presence in the package).

## Why a separate process

The old Python TUI (`prompt_toolkit` competing for stdin, winpty splitting
escape sequences, panels testable only by monkeypatching `console`) had TUI
problems, not Python problems. That rendering layer is gone; moving it to a
mature framework fixed those problems without moving the evidence chain, which
is the one thing this product sells.
