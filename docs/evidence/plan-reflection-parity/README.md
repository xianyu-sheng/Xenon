# plan-reflection parity — one loop, all three phases

`/mode plan-reflection` runs the single kernel loop with every phase enabled:
plan (reviewed for coverage), react (bounded per-step sub-loop), reflect (review
a step before release, and the proposed finish).

## What was run

```bash
python scripts/run_plan_reflection_evidence.py
```

Workspace: fresh git repo with `calc.py` (`add`) and `test_calc.py`.

Task: *add `subtract(a, b)` and a matching test, then run the tests.*

## Result

```text
accepted        true
phase           finalized
finalize_sha    95e01b6b5f09ed6b0e5bb28f3119dcc0235ad2e6
debt            (none)
metrics         steps 7 · tool_calls 5 · events 71 · audit_rounds 1
```

Delivery commit:

```text
95e01b6 xenon: In calc.py add a subtract(a, b) function and a matching test ...
 calc.py      | 4 ++++
 test_calc.py | 6 +++++-
```

The plan review is auditable:

```json
{"kind":"plan_reviewed","payload":{"decision":"accepted","added_steps":0}}
```

A review that adds coverage records `decision="amended"` with `added_steps`; one
that would drop an obligation is rejected with the obligation ids and the
original plan stands.

## Why this run matters beyond the plan

This evidence was regenerated **after the legacy engines were removed from the
turn path** (#290 / #202):
`turn_flow` no longer consults `ENGINE_REGISTRY`, `XENON_RUNTIME=legacy` is gone,
and direct-chat escalation runs the kernel loop. The same task still produces a
verified delivery, so the demolition did not change observable behaviour.

## Invariants covered deterministically

The reviewer windows are model-dependent, so the guarantees live in tests:

- `tests/kernel/test_strategies.py::test_plan_review_cannot_drop_an_obligation`
- `tests/kernel/test_plan_runtime.py::test_plan_review_added_step_actually_runs`
- `tests/kernel/test_plan_runtime.py::test_step_review_corrects_a_failing_step_before_release`
- `tests/kernel/test_plan_runtime.py::test_step_review_cannot_bypass_an_unmet_expectation`
- `tests/test_turn_routing.py` — no engine dispatch, no runtime escape hatch

## Files

- `ledger.jsonl` — 71 events, hash chain intact
- `ledger.jsonl.anchor` — head anchor
- `result.json` — task result projection
- `commit.txt` — the delivery commit the finalizer created
