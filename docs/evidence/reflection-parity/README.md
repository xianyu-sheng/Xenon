# Reflection parity — real model, real ledger

`/mode reflection` runs `ReflectFirst` on the kernel: ActFirst executes, then a
single bounded reflection pass may add one corrective tool call. This directory
is the artifact of one real run (DeepSeek `deepseek/deepseek-v4-pro`), not a
mock.

## What was run

```bash
python scripts/run_reflection_evidence.py
```

Workspace: a fresh git repo with `calc.py` (`add`) and `test_calc.py`.

Task: *add `subtract(a, b)` to `calc.py`, add a test, run the tests.*

## Result

```text
accepted        true
phase           finalized
finalize_sha    0d9b33f87905f5407f7d9e1bdd12c6027eb6cd39
debt            (none)
metrics         steps 5 · tool_calls 4 · events 53 · audit_rounds 1
```

Delivery commit:

```text
0d9b33f xenon: In calc.py add a subtract(a, b) function ...
 calc.py      | 4 ++++
 test_calc.py | 6 +++++-
```

## The reflection pass is auditable

The ledger records the review itself, so "the reflector ran" is a fact and not
a claim:

```json
{"kind": "reflection_proposed",
 "payload": {"action": "finish", "capability": "", "decision": "declined", "round": 1}}
```

Here the reflector reviewed the receipts, found nothing worth another tool call,
and **declined**. Declining did not close anything by itself: delivery still came
from deterministic claim resolution —

```text
claim_resolved  c1 file_changed calc.py        bound to fact-18e93def0683d703
claim_resolved  c2 file_changed test_calc.py   bound to fact-18e93def0683d703
claim_resolved  c3 test_passed  test_calc.py   bound to fact-798ce01604496a3c
control_decision deliver
```

— and only then `finalized`.

## Why a decline is the expected outcome here

The first attempt already read both files, patched them, and ran pytest, so the
correct review is "nothing missing". A correction case is covered
deterministically in `tests/kernel/test_north_star.py`
(`test_reflection_correction_is_recorded_before_the_tool_call`) and
`tests/kernel/test_strategies.py`, where a `call_tool` reflection is injected and
recorded as `decision=correction` before the receipt.

## Files

- `ledger.jsonl` — 53 events, hash chain intact
- `ledger.jsonl.anchor` — head anchor
- `result.json` — task result projection
- `commit.txt` — the delivery commit the finalizer created
