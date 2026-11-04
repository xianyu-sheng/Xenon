# Real TTY — kernel plan rejection (#291, scenario 2)

Date: 2026-11-03
Runtime: `XENON_RUNTIME=kernel` through a real ConPTY (`pywinpty`)
Model: `deepseek/deepseek-v4-flash` (real provider, real tokens)
Workspace: temporary git repo with `baseline.txt` committed as baseline
Task: create `plan_out.txt` with a probe token, then **reject** the plan

## What this proves

A real terminal run, not a mock:

1. `/plan on` routes the deferred-write turn to **kernel plan-only**;
2. the model submits a 4-step plan (inspect baseline, edit plan_out, verify
   both) — no tool executes, no file is written;
3. `/plan reject` records `plan/rejected` and **never executes**;
4. the workspace is untouched afterwards: `plan_out_exists=False`,
   `git status --porcelain` is empty, the last commit is still `baseline probe`.

```text
❌ 计划已驳回；未执行，计划模式保持开启。
❌ 计划已驳回（未执行）          <- /plan status afterwards
```

## Dogfooding discovery (P0, fixed in the same PR)

Recording this transcript uncovered a real REPL bug: after a kernel plan turn,
the steering listener thread kept owning stdin (its `stop()` did not interrupt
the blocking `readline()`), so the next prompt starved for ~25s — plan mode
felt "stuck" and `/plan reject` appeared to do nothing.  Fixed: the listener
now polls with a short `select` timeout so `stop()` takes effect in ~0.2s, and
refuses to start where `select()` cannot poll the stream (Windows) rather than
spawning a zombie stdin owner.  Verified on a real TTY: input responds
immediately after the plan task, and `/plan reject` answers.
