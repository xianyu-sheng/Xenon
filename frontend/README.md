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

- **startup experience**: runner animation, settled mark, welcome card
  (version / loop and its phases / models / first-run hint) and startup
  summary, all from the kernel's `ready` frame
- terminal tab title that animates while a turn runs and holds still while it
  waits for a person (`src/terminalTitle.ts`)
- **All 61 slash commands** run through the kernel and print their output here.
  Command handlers return rich markup; the host strips it at the boundary, so
  nothing arrives as literal `[dim]…[/dim]`.
- inline command completion (`/mo` → `/model`, `/mode`), Tab to cycle,
  Enter to accept — and Enter *runs* once the name is complete
- turn timeline (`think` / `tool` / `observe` / `step` / …), with `Ctrl+O`
  revealing the full kernel text behind each one-line summary
- live status while a turn runs: spinner, phase, steps, tools, a local clock
  and char count, plus a second-by-second refresh of the kernel's own facts
  (`status` every 500 ms)
- results as Markdown with a `● Assistant` anchor, a `💭 N 步 · N 个工具`
  summary, the kernel's evidence status, its undischarged obligations (`debt`)
  and the ledger path
- approval panel with the four kernel answers
  (`本次允许` / `本轮允许` / `全部允许` / `拒绝`, plus cancel), with a
  30-line diff preview
- exit savings report (`session.report`): per-model calls, tokens, cache hit
  rate, cost and savings
- scrollback held in Ink's `<Static>` so earlier output is never redrawn away
- **clickable paths**: file paths and the ledger path become OSC-8 hyperlinks,
  so a written file or a ledger can be opened without copy-paste. Ported from
  the Python `repl/clickable_paths.py`, which was built, tested, and never
  wired to any renderer — this is that wiring, in the layer that renders.

Not yet: a model picker as a list widget and `/resume` history replay in the
terminal. There is no mode picker to build: `/mode` only answers. The underlying commands work; only dedicated widgets are missing.
Diff / execution / evidence panes live in the local dashboard below.

## Local dashboard

`/dashboard` (or `xenon --dashboard`) starts a local web view of the **same**
session inside this process. It is a second renderer, not a second kernel: the
page receives the same frames the terminal reduces, plus read-only query frames
(`workspace.status`, `diff.get`, `ledger.events`, `evidence.chain`, `plan.get`,
`memory.list`, `mcp.status`, `report.get`, `sessions.list`), and can submit the
same four actions the keyboard can (text, command, approval, prompt).

- bound to `127.0.0.1` only, random port, per-process random token in `?t=`;
- every route checks the token (`timingSafeEqual`) and a Host allow-list;
- no extra runtime dependency (`node:http`), and the page is one self-contained
  HTML string — no CDN, no build step of its own;
- `XENON_DASHBOARD=0` disables it;
- SSE events are complete snapshots with a `rev`, so a reconnect never replays
  a partial update;
- `--ui python` does not support it.

Architecture, endpoints and the security model:
[`docs/design/dashboard.md`](../docs/design/dashboard.md).

## Layout

| File | Role |
|---|---|
| `src/protocol.ts` | wire types; mirrors the doc, never invents a field |
| `src/host.ts` | spawns and drives the Python host |
| `src/state.ts` | pure reducer over frames — the part worth testing |
| `src/session.ts` | `SessionController`: submit / commands / approvals / queries, shared by terminal and dashboard |
| `src/dashboard/server.ts` | loopback HTTP + SSE server, token and Host checks, actions and queries |
| `src/dashboard/page.ts` | the self-contained dashboard page (one HTML string) |
| `src/ui.tsx` | rendering only |
| `src/App.tsx` | input handling and wiring |
| `src/brand.ts` | plays the startup animation; `brand-frames.ts` is generated |
| `src/brand-frames.ts` | **generated** by `scripts/brand` — do not hand-edit |
| `src/terminalTitle.ts` | terminal-tab activity (OSC / `process.title`) |
| `src/markdown.ts` | small Markdown reader for the answer pane |
| `src/links.ts` | OSC-8 hyperlinks for paths in output |

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

The animation frames are not hand-written on either side. `scripts/brand` owns
the storyboard and emits both `xenon/utils/_brand_frames.py` and
`src/brand-frames.ts`; run its `render` script after changing the artwork.

## Why a separate process

The old Python TUI (`prompt_toolkit` competing for stdin, winpty splitting
escape sequences, panels testable only by monkeypatching `console`) had TUI
problems, not Python problems. That rendering layer is gone; moving it to a
mature framework fixed those problems without moving the evidence chain, which
is the one thing this product sells.
