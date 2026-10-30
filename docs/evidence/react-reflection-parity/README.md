# react-reflection parity — real model, one implementation with rounds

`/mode react-reflection` uses the **same** `ReflectFirst` strategy as
`/mode reflection`; only the budget differs (3 rounds × 2 calls instead of
1 × 1). No second implementation was added.

## What was run

```bash
python scripts/run_react_reflection_evidence.py
```

Workspace: fresh git repo with `calc.py` (`add`) and `test_calc.py`.

Task: *add `divide(a, b)` raising `ValueError` on zero, cover both the normal and
the error case, run the tests.*

## Result

```text
accepted        true
phase           finalized
finalize_sha    15b3597405f70ac485496e489dd9d3ce2bbf5547
debt            (none)
metrics         steps 5 · tool_calls 4 · events 53 · audit_rounds 1
```

Delivery commit:

```text
15b3597 xenon: In calc.py add a divide(a, b) function that raises ValueError ...
 calc.py      |  6 ++++++
 test_calc.py | 13 ++++++++++++-
```

## The review ran and declined

```json
{"kind":"reflection_proposed",
 "payload":{"action":"finish","decision":"declined","capability":"","round":1}}
```

The model finished the work in one pass, so round 1 had nothing to correct and
reflection stopped there. That is the intended behaviour: a decline ends all
remaining rounds rather than re-reading the same receipts twice more.

## What this run does not prove

Multi-round execution is **not** exercised here — it needs a first attempt that
actually leaves a gap, which is model-dependent. That path is covered
deterministically instead:

- `tests/kernel/test_strategies.py::test_multi_round_reflection_stops_at_the_round_budget`
  (3 reviews, 1 call each, then stop);
- `tests/kernel/test_strategies.py::test_per_round_budget_allows_several_calls_in_one_round`
  (2 rounds × 2 calls);
- `tests/kernel/test_strategies.py::test_repeated_correction_ends_reflection_immediately`
  (loop detection);
- `tests/kernel/test_north_star.py::test_multi_round_reflection_records_each_round`
  (ledger records rounds 1→2→3, and delivery still comes from verification).

## Files

- `ledger.jsonl` — 53 events, hash chain intact
- `ledger.jsonl.anchor` — head anchor
- `result.json` — task result projection
- `commit.txt` — the delivery commit the finalizer created
