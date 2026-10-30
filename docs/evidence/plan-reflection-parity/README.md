# plan-reflection parity — one loop, all three phases

`/mode plan-reflection` is the single kernel loop with every phase enabled:

1. **plan** — propose one plan, and review its coverage before submitting;
2. **react** — work the active step in a bounded sub-loop;
3. **reflect** — review a step before it is released, and the proposed finish.

There is no second strategy class for this mode. `LoopProfile` only says which
phases run and how much they may spend.

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
finalize_sha    44b16720a97ecf4d7c9652c12fe32e557597f0ae
debt            (none)
metrics         steps 8 · tool_calls 6 · events 82 · repair_attempts 1 · audit_rounds 1
```

Delivery commit:

```text
44b1672 xenon: In calc.py add a subtract(a, b) function and a matching test ...
 calc.py      | 4 ++++
 test_calc.py | 6 +++++-
```

`repair_attempts=1` is the per-step review path doing its job: one step produced
work that did not satisfy its expectation, the step stayed open, and the
correction happened inside that step instead of after the whole plan ran.

## The plan review is auditable

```json
{"kind":"plan_reviewed","payload":{"decision":"accepted","added_steps":0}}
```

The reviewer read the proposed plan and found nothing missing. A review that adds
coverage records `decision="amended"` with `added_steps`; one that would drop an
obligation is rejected with `decision="rejected"` and the obligation ids, and the
original plan stands.

## Invariants proven by tests, not by this run

The reviewer's windows are model-dependent, so these are covered
deterministically:

- `tests/kernel/test_strategies.py::test_plan_review_cannot_drop_an_obligation`
  — a plan review may add steps and expects, never remove them;
- `tests/kernel/test_plan_runtime.py::test_plan_review_added_step_actually_runs`
  — an added step really executes;
- `tests/kernel/test_plan_runtime.py::test_step_review_corrects_a_failing_step_before_release`
  — a step that has not closed is corrected in place;
- `tests/kernel/test_plan_runtime.py::test_step_review_cannot_bypass_an_unmet_expectation`
  — a review that finds nothing still ends in debt, never delivery;
- `tests/kernel/test_strategies.py::test_profile_rejects_incoherent_budgets`
  — an incoherent profile is rejected at construction.

## Two real bugs this slice found earlier

1. **`verify` prefixed with `cd`** — `cd /d <path> && pytest` turned a plain
   verify into a permission-gated shell command, so the run ended `BLOCKED`.
   The prompts now forbid `cd` in verify commands, since the workspace root is
   already the cwd.
2. **Plan review logged as reflection** — the first successful run recorded the
   plan review as `reflection_proposed` with `round: 0`; it is now its own
   `plan_reviewed` event.

## Files

- `ledger.jsonl` — 82 events, hash chain intact
- `ledger.jsonl.anchor` — head anchor
- `result.json` — task result projection
- `commit.txt` — the delivery commit the finalizer created
