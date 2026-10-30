# PlanReact parity — real model, per-step sub-loops

`/mode plan-react` runs `PlanReact` on the kernel: submit one global plan, then
work the active step in a bounded sub-loop. This directory is one real run
(DeepSeek `deepseek/deepseek-v4-pro`).

## What was run

```bash
python scripts/run_plan_react_evidence.py
```

Workspace: fresh git repo with `calc.py` (`add`) and `test_calc.py`.

Task: *add `multiply(a, b)` and `power(a, b)` to `calc.py`, one test each, run
the tests.*

## Result

```text
accepted        true
phase           finalized
finalize_sha    16cd5b398eefe956808c9cac9d3f428e986db42b
debt            (none)
metrics         steps 7 · tool_calls 5 · events 70 · audit_rounds 1
```

Delivery commit:

```text
16cd5b3 xenon: In calc.py add a multiply(a, b) function, and also a power(a, b) ...
 calc.py      |  8 ++++++++
 test_calc.py | 10 +++++++++-
```

## Steps ran one at a time, in order

```text
step_started 1  read  calc.py          → step_closed 1 satisfied
step_started 2  read  test_calc.py     → step_closed 2 satisfied
step_started 3  edit  expects e1       → step_closed 3 satisfied (v1 fact)
step_started 4  edit  expects e2       → step_closed 4 satisfied (v1 fact)
step_started 5  verify expects e3      → step_closed 5 satisfied (v1 fact)
```

Each step carries its own receipt, its own verdict and its own obligation
binding (`e1`/`e2`/`e3`), so a later step can never borrow an earlier step's
evidence.

## Bounds are visible

`STEP_EXPLORED` is absent here: no step needed the per-step budget, so the actor
never hit `budget_exhausted` or `step_complete` early. Both paths are covered
deterministically in:

- `tests/kernel/test_strategies.py` — per-step budget, reset on the next step,
  step completion never becomes `Finish`;
- `tests/kernel/test_plan_runtime.py` — `test_plan_react_step_budget_is_visible_in_the_ledger`
  (exhausted budget ⇒ `STEP_EXPLORED` + debt, never delivery),
  `test_plan_react_never_closes_a_step_without_its_expectation`.

## Bug found by this run

The first attempt produced `plan_proposed` with
`analysis="invalid plan proposal: unknown capability: 'bash'"` — the plan
protocol never enumerated the allowed `capability` values, so a tool name
invalidated the whole plan. Fixed in the same PR: the protocol now lists
`read, search, edit, verify, vcs, shell, remote`, and `parse_plan` maps common
tool-name aliases (`bash`, `grep`, `apply_patch`, …) instead of failing closed on
them.

## Files

- `ledger.jsonl` — 70 events, hash chain intact
- `ledger.jsonl.anchor` — head anchor
- `result.json` — task result projection
- `commit.txt` — the delivery commit the finalizer created
