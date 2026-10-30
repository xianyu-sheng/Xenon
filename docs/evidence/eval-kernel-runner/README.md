# Eval runner on the shipped kernel loop (#202)

`evals/swebench_xenon.py` used to construct legacy engines and call
`engine.run(...)`. It now calls `evals.kernel_runner.run_eval_task`, which runs
the same prompt through `run_task` — the loop that ships.

That matters for honesty: the numbers in the README come from the runtime users
get, not from a retired code path.

## What the runner supplies vs what the kernel owns

| supplied by the eval | owned by the kernel |
|---|---|
| prompt, worktree, model config | ledger, task state machine |
| loop profile (`react`, `plan-react`, …) | catalog binding, gateway execution |
| sandbox `ToolRuntime` (injected) | verification, debt, delivery decision |

The sandbox runtime is injected through `LegacyToolGateway(runtime=...)`, so
tools execute inside the eval container while the kernel still decides *what*
runs. `finalize=False` keeps `git diff` as the graded patch.

## Real run

DeepSeek `deepseek-v4-pro`, fresh git repo with `calc.py` + `test_calc.py`.

Task: *add `divide(a, b)` raising `ValueError` on zero, add `test_divide`, run
`python -m pytest test_calc.py -q`, and only claim `test_passed` if it passes.*

```text
accepted     true
phase        finalized
debt         (none)
finalize_sha null   ← evals grade the working tree, not a commit
tools        read, read, apply_patch, apply_patch, bash
ledger       73 events, chain valid=True
```

Patch (excerpt):

```diff
+def divide(a, b):
+    if b == 0:
+        raise ValueError("division by zero")
+    return a / b
```

## Fail-closed behaviour observed before this pass

An earlier attempt claimed `test_passed` for a target the test run did not
cover. The kernel refused to deliver (`phase=blocked`,
`debt=("no passing test fact for target",)`) instead of accepting the claim. That
is the behaviour the eval wants from the harness: a wrong claim is a blocked
task, not a silently inflated score.

## Files

- `ledger.jsonl` — kernel ledger for the run
- `ledger.jsonl.anchor` — head anchor
- `patch.diff` — graded working-tree diff
- `result.json` — acceptance/phase/tool summary
