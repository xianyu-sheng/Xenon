# Real model + real MCP through the REPL kernel

Date: 2026-10-27  
Runtime: `XENON_RUNTIME=kernel`  
Model: `deepseek/deepseek-v4-pro`  
MCP: configured `github-mcp-server` (92 tools discovered)  
Approval: explicit evidence override (`_confirm_tool` -> approve); non-TTY
production behavior remains fail-closed.

## Task

```text
Use the configured GitHub MCP server to search repositories for
"xianyu-sheng/Xenon" (read-only). Tell me the repository full_name and star
count you actually found. Do not modify anything.
```

## Result

```json
{
  "accepted": true,
  "answer": "Using the read-only GitHub MCP search_repositories call for \"xianyu-sheng/Xenon\", I found exactly one repository: full_name = \"xianyu-sheng/Xenon\" (name \"Xenon\", owner \"xianyu-sheng\"), and it has 56 stars (stargazers_count: 56). Other observed fields: language Python, 12 forks, 45 open issues, default branch main, not a fork, not archived, created 2026-06-03. Nothing was modified; this was a read-only remote query.",
  "claims": [],
  "debt": [],
  "evidence_status": "verified",
  "finalize_sha": null,
  "metrics": {
    "events": 24.0,
    "repair_attempts": 0.0,
    "replans": 0.0,
    "steps": 2.0,
    "tool_calls": 1.0
  },
  "phase": "finalized",
  "resolutions": [],
  "schema_version": "kernel/v1",
  "task_id": "repl-29fc9742c7cc",
  "verdicts": [
    {
      "fact_ids": [],
      "obligation_id": "task",
      "reason": "",
      "stage": "v2",
      "status": "satisfied"
    }
  ]
}
```

- accepted: `True`
- evidence: `verified`
- phase: `finalized`
- steps: `2.0`
- tool_calls: `1.0`
- claims: `0` (remote query correctly finished with no claims)

The model called `github:search_repositories`, received the real MCP result, and
answered with:
`full_name = "xianyu-sheng/Xenon"`, `stargazers_count = 56`.

## Ledger

- `policy_decision: require_approval` scope includes `github:search_repositories`
- `user_approval: approved=true`
- `tool_receipt`: MCP NetworkFact with `mcp://github:search_repositories`
- `control_decision: deliver`
- `finalized: evidence_status=verified`

`ledger.jsonl` + `ledger.anchor` verify locally (`valid=True`).

## Notes

- The temp workspace is not a git repo, so `finalize_sha` is null; this was a
  read-only query, not a delivery commit.
- MCP content is untrusted; it was bounded to 8000 chars in the ledger/view.
- This is a real model + real MCP call, but a **simulated TTY approval**; the
  real non-TTY path remains fail-closed.
