# Real REPL kernel runtime evidence — repl-7f4451ac9174

Date: 2026-10-27  
Runtime: `XENON_RUNTIME=kernel`  
Model: `deepseek/deepseek-v4-pro`  
Workspace: temporary git repo (calculator.py)  
Entry: `REPL._handle_chat(...)` (the same turn pipeline as interactive input)

## Command

```bash
XENON_RUNTIME=kernel xenon   # then enter:
# Fix calculator.py so add(2, 3) returns 5. Run tests/test_calculator.py with pytest. Do not commit; the harness finalizer will commit verified files.
```

For this non-TTY evidence run, the same `REPL` object was constructed with a
`ModelRegistry` and `_handle_chat(...)` was called directly. The output is in
`repl.out`.

## Result

```json
{
  "accepted": true,
  "answer": "Changed add() in calculator.py to return a + b so add(2, 3) returns 5. Ran `python -m pytest tests/test_calculator.py -q`; result: 1 passed, 0 failed.",
  "claims": [
    {
      "expect": {},
      "id": "c1",
      "kind": "file_changed",
      "since_seq": 1,
      "source": "llm",
      "target": "calculator.py"
    },
    {
      "expect": {},
      "id": "c2",
      "kind": "test_passed",
      "since_seq": 1,
      "source": "llm",
      "target": "tests/test_calculator.py"
    }
  ],
  "debt": [],
  "evidence_status": "verified",
  "finalize_sha": "866a38366ea842bcb3b211419dc603f62358fbc0",
  "metrics": {
    "events": 48.0,
    "repair_attempts": 0.0,
    "replans": 0.0,
    "steps": 5.0,
    "tool_calls": 4.0
  },
  "phase": "finalized",
  "resolutions": [
    {
      "candidates": [],
      "claim_id": "c1",
      "fact_ids": [
        "fact-38404336076dc873"
      ],
      "reason": "",
      "status": "bound"
    },
    {
      "candidates": [],
      "claim_id": "c2",
      "fact_ids": [
        "fact-bb9814dd7404df58"
      ],
      "reason": "",
      "status": "bound"
    }
  ],
  "schema_version": "kernel/v1",
  "task_id": "repl-7f4451ac9174",
  "verdicts": [
    {
      "fact_ids": [
        "fact-38404336076dc873"
      ],
      "obligation_id": "c1",
      "reason": "",
      "stage": "v2",
      "status": "satisfied"
    },
    {
      "fact_ids": [
        "fact-bb9814dd7404df58"
      ],
      "obligation_id": "c2",
      "reason": "",
      "stage": "v2",
      "status": "satisfied"
    }
  ]
}
```

Key metrics:

- phase: `finalized`
- accepted: `True`
- evidence_status: `verified`
- steps: `5.0`
- tool_calls: `4.0`
- finalize_sha: `866a38366ea842bcb3b211419dc603f62358fbc0`
- claims resolved: `['bound', 'bound']`

## REPL projection

`repl.out` shows:

```text
🧠 Runtime: kernel (deepseek/deepseek-v4-pro)
  💭 kernel · 5 steps · 4 tools · verified
● Kernel
  ...
  交付: commit 866a38366ea8 · evidence: verified
```

The turn tree ended `passed` with `engine=kernel`; the legacy engine host was
not called. The pre-turn legacy `_task_state` was cleared after acceptance.

## Commit

```text
866a383 xenon: Fix calculator.py so add(2, 3) returns 5. Run tests/test_calculator.py w
 calculator.py | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)

```

## Ledger

`ledger.jsonl` + `ledger.anchor` contain chained events; verified locally with
`Ledger(...).verify()` (`valid=True`). The result sidecar was written by
`RuntimeAdapter` next to the ledger.
