# Headless harness (`xenon exec`)

`xenon exec` is the deterministic, non-interactive entry into Xenon's kernel.
It runs in a real workspace, uses the real `ToolExecutor`, writes a durable
ledger, applies the permission policy, and creates a delivery commit through
`GitFinalizer`.

## Natural-language task

```bash
xenon exec "add an add() function to calc/add.py and test it" \
  -m deepseek/deepseek-v4-pro \
  --workspace . \
  --output result.json
```

`xenon exec --task "..."` is the explicit equivalent. Without `-m`, the
harness resolves a model from `~/.xenon/models.yaml`
(preferring the `planner` role), then falls back to the first provider model in
`~/.xenon/credentials.yaml`. Use `--config path/to/models.yaml` to point at a
different model file. A missing model or API key is a configuration error
(exit code 2), not a crash.

The model returns one capability action per turn:

- `call_tool` with `capability` (`read`, `search`, `edit`, `verify`, `shell`,
  `vcs`, `remote`), `params` and optional `targets`;
- `think`, `ask_user`, `abort`, or `finish` with optional claims.

The model never sees or invokes a concrete tool. The catalog binds the
capability; the runtime applies policy, records a receipt, and verifies claims.
Malformed model actions become a visible `think` turn and are bounded by
`--max-steps`.

The model must not commit or push. When it finishes, the finalizer stages only
files with write/delete receipts from this run and creates the delivery commit.
A model claim that cannot be resolved from facts blocks delivery.

## Deterministic plan task

```bash
xenon exec --plan plan.json --workspace . --output result.json
```

Plan steps use `kind` + `capability` + `params` + `expects` + optional
`depends_on`; no step names a concrete tool. This path is intended for
regression tests, benchmarks, and reproducible delivery checks.

Example plan shape:

```json
{
  "objective": "add and test add()",
  "steps": [
    {
      "id": 1,
      "kind": "edit",
      "capability": "edit",
      "params": {"files": [{"path": "calc/add.py", "content": "..."}]},
      "expects": [{"id": "edit-1", "kind": "file_changed", "target": "calc/add.py"}]
    },
    {
      "id": 2,
      "kind": "verify",
      "capability": "verify",
      "params": {"command": "python -m pytest tests/test_add.py -q"},
      "depends_on": [1],
      "expects": [{"id": "test-1", "kind": "test_passed", "target": "tests/test_add.py"}]
    }
  ]
}
```

## Output and exit codes

- `--jsonl` (default): one canonical ledger event per stdout line.
- `--no-jsonl`: suppress the event stream.
- `--output result.json`: write the final `TaskResult` JSON.
- Exit `0`: accepted and delivered.
- Exit `1`: blocked, aborted, or evidence/debt incomplete.
- Exit `2`: plan/model/usage configuration error.
- Exit `3`: harness or provider failure.

Keep the ledger directory outside the workspace. The finalizer treats unknown
worktree paths as unverified and will reject the run, which is intentional.
