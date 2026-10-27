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

## Interactive REPL (opt-in kernel runtime)

For one release the REPL keeps both paths:

```bash
# legacy engine host (default)
xenon

# task turns enter the kernel runtime
XENON_RUNTIME=kernel xenon
```

Only turns that require tools and are not a skill, plan-mode turn, pending
continuation, or atomic recipe are routed to the kernel in this first slice.
External queries stay legacy unless an MCP registry is available; then the
kernel uses the `core+mcp` profile and the remote call goes through approval.
Plain chat, `/mode`, `/plan`, `/resume`, `/evidence`, and skills still use their
existing paths.

The REPL adapter:

- builds a `TaskSpec` from the turn prompt and the selected model;
- uses the workspace root from `ProjectContext`;
- writes its durable ledger and `result.json` sidecar under
  `~/.xenon/ledger/repl/`;
- maps `READ_ONLY` to `PermissionMode.READ_ONLY` and
  `WRITE`/`EXECUTE` to `PermissionMode.AUTO_EDIT` (no automatic bypass);
- runs `GitFinalizer` only when the workspace has `.git`;
- projects kernel events into the existing ThinkingPanel and writes the answer
  back to `ContextManager` and the turn tree.

If the kernel blocks a turn, the REPL prints the open obligations and the
ledger path. `XENON_RUNTIME` is a migration flag, not a second permanent
runtime; it will be removed after parity (#202).

## Approval and scoped grants

The kernel can produce a `require_approval` policy decision, but it never
prompts by itself. Approval is only active when an `ApprovalProvider` is
injected:

- REPL `XENON_RUNTIME=kernel` bridges the decision to the existing
  `_confirm_tool`/approval panel for `EXECUTE` turns;
- an approval is converted into a **one-shot `ScopedGrant`** for the exact
  command/target and is revoked after the gateway call;
- no provider, non-TTY, user denial, or a grant that cannot be scoped all fail
  closed with a `USER_APPROVAL` ledger event;
- destructive commands (`reset --hard`, force push, `rm -rf`, ...) and any
  `git push` remain `VETO`, not approvable until an explicit pre-execution
  `TaskSpec.allow_push` gate exists;
- `xenon exec` does not inject an approval provider, so headless runs keep the
  conservative veto behavior.

`READ_ONLY` never becomes an approval prompt. `WRITE` turns do not gain shell
approval; only `EXECUTE` maps to the approval-capable policy.

## MCP read-only slice

When the REPL has an MCP registry (connected or lazily configured), the kernel
runtime uses the `core+mcp` catalog profile:

- the gateway receives the REPL `AgentContext`, so `mcp_call` can reach
  `_mcp_registry`;
- available MCP tool names are appended to the task constraints when already
  discovered;
- `mcp_call` uses `tool_name` + `tool_args`;
- every remote call still requires approval;
- the result is recorded as `NetworkFact(url="mcp://<tool>", content=...)` and
  the bounded content is visible in the model working set.

External queries are routed to the kernel only when an MCP registry is
available; otherwise they stay on the legacy path. MCP servers are not sandboxed
by Xenon's in-process sandbox; approval is the boundary. Dynamic per-tool
`ToolSpec` discovery and real GitHub MCP transcripts are follow-ups.
