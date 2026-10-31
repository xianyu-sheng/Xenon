# Child runtime — same loop, isolated ledger (#204 minimal)

`/sub-agent` used to construct `ReActEngine` and call `_spawn_subagent`. It now
runs `xenon.harness.child.run_child_task`, which is the **same** `run_task` loop
with its own ledger, its own objective and **no delivery commit**.

A child is not a second execution model: it is the same phases (plan / react /
reflect) selected by a child profile, writing to a separate ledger so the parent
never shares mutable task state.

| child profile | loop profile |
|---|---|
| `react` | bare react |
| `plan-execute` | plan + follow intents |
| `plan-react` / `kernel` | plan + per-step sub-loop |
| `reflection` | react + 1×1 reflection |

Depth is structurally 1: the loop has no spawn tool, so a child cannot recurse.

## What was run

DeepSeek `deepseek-v4-pro`, fresh git repo with `calc.py` + `test_calc.py`.

Task: *add `multiply(a, b)`, add `test_multiply`, run pytest, claim `test_passed`
for `test_calc.py` only if it passes.*

```text
accepted     true
phase        finalized
delivered    true
tool_calls   5
ledger       evidence-parent-sub1-26c941.jsonl  (73 events, chain valid=True)
commits      edf053b init        ← only the baseline; the child created none
```

Patch really landed in the child worktree:

```diff
+def multiply(a, b):
+    return a * b
```

## Why `finalize=False` matters

A child must not own delivery. The parent (or the user) decides whether to commit
and which finalizer path applies. Here the child produced a verified working
tree and left committing to the caller — visible above as an unchanged `git log`.

## Parent evidence is a pointer

`child_evidence(outcome)` records `child_id`, objective, profile, verdict,
`ledger` path, debt, tool-call count and elapsed time in the **session** event
stream. The parent ledger is untouched: the kernel stays single-writer, and a
child's events are never copied into it.

## Files

- `ledger.jsonl` — the child's own kernel ledger (73 events)
- `ledger.jsonl.anchor` — head anchor
- `patch.diff` — what the child actually changed
- `evidence.json` — the pointer payload the parent records
