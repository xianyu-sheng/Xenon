# Real `xenon exec` evidence — real-exec-2

Date: 2026-10-27  
Model: `deepseek/deepseek-v4-pro`  
Workspace: temporary git repo (calculator.py)  
Command:

```bash
xenon exec "Fix calculator.py so add(2, 3) returns 5. Run tests/test_calculator.py with pytest and finish. Do not commit; the harness finalizer will commit verified files." \
  -m deepseek/deepseek-v4-pro \
  --workspace <tmp>/repo \
  --ledger-dir <tmp>/ledger2 \
  --task-id real-exec-2 \
  --jsonl --output result.json --max-steps 12
```

## Result

```json
{
  "accepted": true,
  "answer": "Fixed calculator.py so add() returns a + b instead of a - b. Verified with pytest: tests/test_calculator.py passed (1 passed, exit code 0).",
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
  "finalize_sha": "4723dd37248cc93154133b1bbd6c69457afba5fb",
  "metrics": {
    "events": 41.0,
    "repair_attempts": 0.0,
    "replans": 0.0,
    "steps": 4.0,
    "tool_calls": 3.0
  },
  "phase": "finalized",
  "resolutions": [
    {
      "candidates": [],
      "claim_id": "c1",
      "fact_ids": [
        "fact-f1a6ab0390334c31"
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
  "task_id": "real-exec-2",
  "verdicts": [
    {
      "fact_ids": [
        "fact-f1a6ab0390334c31"
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
- steps: `4.0`
- tool_calls: `3.0`
- finalize_sha: `4723dd37248cc93154133b1bbd6c69457afba5fb`
- claims resolved: `['bound', 'bound']`

## Commit

```text
4723dd3 xenon: Fix calculator.py so add(2, 3) returns 5. Run tests/test_calculator.py w
 calculator.py | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)

```

## Ledger

`ledger.jsonl` has 41 chained events and `ledger.anchor`; verification was run
locally with `Ledger(path, "real-exec-2").verify()` and returned `valid=True`.

Key sequence:

1. `read calculator.py` → ReadFact with content and hash;
2. `apply_patch` → FsWrite fact for `calculator.py`;
3. `bash pytest tests/test_calculator.py -q` → TestRun (1 passed, exit 0);
4. `finish` with `file_changed` + `test_passed` claims;
5. finalizer creates commit `4723dd3` from verified paths;
6. V2 claims resolve as `bound`, evidence `verified`.

## Why the first run failed (and what it proved)

The first real run was blocked after six read calls:

- the model could see `path` and `content_hash`, but not the read content;
- it tried `shell: cat calculator.py` and the permission policy vetoed it.

That identified a real projection gap, fixed in `ReadFact.content` /
ModelView. The successful transcript above is the post-fix run.
