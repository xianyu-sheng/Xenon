# Evidence trust: threat model and optional anchors

## Threat model

Xenon's default chain is tamper-evident, not a proof against a writer:

- An unkeyed SHA-256 chain detects accidental corruption, reorder/truncation
  and single-point edits.
- An attacker with write access can rewrite the chain and recompute every
  plain hash.  That is why keyed mode exists.
- The ledger is still not a substitute for an external secret store or a
  trusted timestamp authority.

## Keyed mode

`XENON_EVIDENCE_HMAC_KEY` (optional, environment only, never stored in a
payload or snapshot) switches session evidence hashes to
`HMAC-SHA256(key, prev ‖ hk ‖ body)`.  Verification without the expected key
returns `partial`, not `ok`; stripping all keyed markers from a chain also
returns `partial` when a key is expected.  Key rotation means starting a new
session/chain with the new key; old chains remain readable as `unkeyed` or
`partial` and must not be upgraded in place.

Existing regression coverage: `tests/test_evidence_chain.py` (tamper, reorder,
re-hash, downgrade, mixed mode, weak-key warning).

## External anchor

`EvidenceLedger.write_anchor(path)` writes a tiny JSON sidecar:

```json
{"session_id": "...", "event_count": 12, "head_hash": "..."}
```

`verify_anchor(path)` compares it with the live ledger.  Append, truncate,
reorder or a rewritten head fails unless the anchor is updated by the trusted
operator too.  This is deliberately an interface/sidecar, not a second state
store; CI or an operator can persist it outside the workspace.  No secrets or
payloads are included.

## Full failure references

Large tool output is stored in the session tool-result cache and represented
in observations by a stable `result_id` plus summary/hash metadata.  The
ledger therefore keeps a reference, not raw stdout/file contents; retrieval
happens through `tool_result_read(result_id=...)`, where credentials are
redacted by the existing result-store boundary.

Tests: `tests/test_evidence_anchor.py`, `tests/test_tool_result_*` and the
keyed-chain regression suite.
