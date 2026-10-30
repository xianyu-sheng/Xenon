# plan-reflection parity — review the plan, review each step

`/mode plan-reflection` is `PlanReact` plus two review seams:

1. **plan coverage review** before the plan is submitted — looks for work the
   objective needs but the plan never does (missing verify step, untouched error
   path);
2. **per-step review** before a step is released — so a mistake is fixed where
   it happened instead of after the whole plan has run.

Both reviewers are proposal sources only, and one invariant is enforced in code:
a plan review may **add** steps and expects, never remove an obligation.

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
finalize_sha    cdadd0444db2d0deec32d04ba33de121cb6d9e41
debt            (none)
metrics         steps 7 · tool_calls 5 · events 71 · audit_rounds 1
```

Delivery commit:

```text
cdadd04 xenon: In calc.py add a subtract(a, b) function and a matching test ...
 calc.py      | 4 ++++
 test_calc.py | 6 +++++-
```

## The plan review is auditable

```json
{"kind":"plan_reviewed","payload":{"decision":"accepted","added_steps":0}}
```

The reviewer read the proposed 5-step plan (read → read → edit → edit → verify)
and found nothing missing, so the plan went through unchanged. A review that adds
coverage shows up as `decision="amended"` with `added_steps`, and one that would
drop an obligation is rejected with `decision="rejected"` and the obligation ids.

`step_explored` is absent in this run: no step failed or ran out of budget, and
the per-step review only has a window while a step is still open.

## Two real bugs this slice found

1. **`verify` prefixed with `cd`** — `cd /d <path> && pytest` turned a plain
   verify into a permission-gated shell command, so the run ended `BLOCKED`
   (`"verify requires a grant"`). The workspace root is already the cwd, so the
   prompts now forbid `cd` in verify commands.
2. **Plan review logged as reflection** — the first successful run recorded the
   plan review as `reflection_proposed` with `round: 0`. That was semantically
   wrong, so `plan_reviewed` is now its own event.

Neither run was massaged: the blocked attempt is what forced the prompt fix, and
the wrong event was replaced by a dedicated one.

## What this run does not prove

The per-step review window is model-dependent: it opens only while a step is
still open (a violated receipt, or unmet expectations). Deterministic coverage:

- `tests/kernel/test_plan_runtime.py::test_step_review_corrects_a_failing_step_before_release`
  (a read in a verify step stays open, review injects the real `verify`, step
  closes satisfied, no extra step started);
- `tests/kernel/test_plan_runtime.py::test_step_review_cannot_bypass_an_unmet_expectation`
  (review that finds nothing still ends in debt, not delivery);
- `tests/kernel/test_plan_runtime.py::test_plan_review_added_step_actually_runs`;
- `tests/kernel/test_strategies.py::test_plan_review_cannot_drop_an_obligation`.

## Files

- `ledger.jsonl` — 71 events, hash chain intact
- `ledger.jsonl.anchor` — head anchor
- `result.json` — task result projection
- `commit.txt` — the delivery commit the finalizer created
